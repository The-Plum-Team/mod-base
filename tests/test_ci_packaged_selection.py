"""The exact Build of a packaged run: three routes, one record, and the check before use.

Only the API is faked. Job listings are the literal ones of ``tests/fixtures/ci_graphs``, bundles
are real ZIPs, and every download is extracted, verified and published on the real filesystem
(Linux).
"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mod_base.build_ci import selection
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.records import bind_source_selection, build_source_selection, validate_source_selection
from mod_base.errors import MbError
from mod_base.github.api import ApiError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_sha256
from tests.helpers import (CI_WORKFLOWS, ci_descriptor, ci_envelope, ci_graph_jobs, ci_plan, ci_run_descriptor,
                           ci_selection, ci_staged_plan)
from tests.test_ci_build_selection import later_run
from tests.test_ci_transport import ASSEMBLE, GATE, World, after_download, build_archive, build_world, exported

LISTING = "/repos/example/mod/actions/workflows/mod-base-build.yml/runs"
PACKAGED = CI_WORKFLOWS["packaged"]
GUARD = "Verify pinned mod-base / Authenticate the pinned kit"
SELECT = "Select exact Build / Select exact Build source"
PLAN = "Shared Build / Plan protected Build"
POLICY = "Shared Build / Verify protected policy"
TARGET = "Shared Build / Compile target target-a"
INPUT = "Shared Packaged E2E / Authenticate exact Build"
#: The jobs of the packaged half of a rebuilt run that do not exist yet while its input job reads.
LATER = ("Shared Packaged E2E / Run packaged lane lane-a", "Shared Packaged E2E / Seal complete packaged results",
         "Shared Packaged E2E / Verify complete packaged E2E")


def repin(world, run_id, index):
    """Make a run name another controller commit (index 0) or another kit pin (index 1)."""

    entry = world.runs[run_id]["referenced_workflows"][index]
    entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)
    world.api.add_run(world.runs[run_id])


def dispatched_world():
    """A protected subject whose finished full Build run was started by a manual dispatch."""

    world = World(push=True)
    world.add_run("build", "build-full", event="workflow_dispatch")
    record = {**ci_run_descriptor(world.plan, "build", "full", "build")["producer"], "event": "workflow_dispatch"}
    data, world.envelope = build_archive(world.plan, record)
    descriptor = world.describe("build", "full", "build", data)
    descriptor["producer"]["event"] = "workflow_dispatch"
    world.bundle = world.publish(descriptor, data)
    return world


def rebuild(world, *, running=True):
    """Give a protected subject a standalone packaged run that found no Build and built in its own
    run. While it runs, its ``input`` job is the reader: the Build half has finished and the lanes
    do not exist yet."""

    if not running:
        world.add_run("packaged", "packaged-rebuilt")
    else:
        world.add_run("packaged", "packaged-rebuilt", status="in_progress", conclusion=None)
        jobs = [job for job in world.jobs[43] if job["name"] not in LATER]
        reader = next(job for job in jobs if job["name"] == INPUT)
        reader.update(status="in_progress", conclusion=None, completed_at=None)
        reader["steps"] = reader["steps"][:1]
        world.set_jobs(43, jobs)
    world.add_bundle("packaged", "rebuilt")
    return world


def rebuilt_world(*, running=True):
    return rebuild(World(push=True), running=running)


def published(test, world, output):
    """Require ``output`` to be the verified private copy of the world's bundle."""

    test.assertEqual(verify_build_export(output, plan=world.plan), world.envelope)
    test.assertEqual(exported(output), sorted([grammar.CI_ENVELOPE_NAME,
                                               *(file["path"] for file in world.envelope["files"])]))


class SelectionRecordTests(unittest.TestCase):
    """The writer of ``mod-base.ci.selection`` and the binding its consumer repeats (no API)."""

    def build(self, **changes):
        arguments = {"plan": ci_plan(), "request": ci_selection()["request"], "build": ci_descriptor(),
                     "envelope": ci_envelope(), **changes}
        return build_source_selection(**arguments)

    def bind(self, document, **changes):
        arguments = {"plan": ci_plan(), "run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED, **changes}
        return bind_source_selection(document, **arguments)

    def test_the_writer_produces_the_canonical_fixture_record(self):
        before = (ci_plan(), ci_selection()["request"], ci_descriptor(), ci_envelope())
        arguments = copy.deepcopy(before)
        record = build_source_selection(plan=arguments[0], request=arguments[1], build=arguments[2],
                                        envelope=arguments[3])
        self.assertEqual(record, ci_selection())
        self.assertEqual(arguments, before)
        record["build"]["artifact"]["id"] += 1  # the record shares nothing with what it was built from
        self.assertEqual(arguments, before)
        self.assertEqual(self.bind(ci_selection()), ci_selection())

    def test_the_writer_refuses_an_envelope_or_a_request_that_is_not_this_selection(self):
        request, envelope, descriptor = ci_selection()["request"], ci_envelope(), ci_descriptor()
        other_plan = ci_staged_plan()
        cases = [{"envelope": {**envelope, "plan_sha256": "f" * 64}},
                 {"envelope": {**envelope, "producer": {**envelope["producer"], "run_attempt": 1}}},
                 {"envelope": ci_envelope(target_id="target-a")}, {"envelope": {}},
                 {"build": ci_descriptor("target", unit_id="target-a")},
                 {"build": ci_descriptor("tested", unit_id="build")},
                 {"build": {**descriptor, "plan_sha256": "f" * 64}}, {"plan": other_plan},
                 {"request": {**request, "nonce": "0" * 63}}, {"request": {**request, "run_id": 0}},
                 {"request": {key: value for key, value in request.items() if key != "nonce"}},
                 {"request": {**request, "extra": 1}}, {"request": None},
                 {"request": {**request, "workflow_ref": request["workflow_ref"].replace("master", "other")}},
                 {"request": {**request, "workflow_path": CI_WORKFLOWS["build"],
                              "workflow_ref": request["workflow_ref"].replace("packaged-e2e", "build")}},
                 {"request": {**request, "run_id": 42}}]
        for index, changes in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(MbError):
                self.build(**changes)

    def test_only_the_requesting_attempt_of_the_packaged_caller_binds_a_record(self):
        for changes in ({"run_id": 44}, {"run_attempt": 1}, {"run_attempt": 3}, {"workflow_path": CI_WORKFLOWS["build"]},
                        {"workflow_path": ".github/workflows/mod-base-gate-status.yml"}, {"plan": ci_staged_plan()}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.bind(ci_selection(), **changes)
        document = ci_selection()
        document["request"]["run_id"] = 42  # the consuming run would be the Build run itself
        with self.assertRaisesRegex(MbError, "separate run"):
            self.bind(document, run_id=42)


class ProtectedSelectionTests(unittest.TestCase):
    def call(self, world):
        return selection.select_protected_build(world.api, plan=world.plan)

    def test_newest_push_or_dispatch_build_is_described_from_api_data(self):
        for event in ("push", "workflow_dispatch"):
            world = build_world(push=True, event=event)
            expected = copy.deepcopy(world.bundle)
            expected["producer"]["event"] = event
            with self.subTest(event=event):
                self.assertEqual(self.call(world), expected)
                # The subject (default branch, its head, the commit), the listing, the run, its jobs,
                # the bundle listing and the bundle record.
                self.assertEqual(world.api.request_count, 8)
                self.assertEqual(world.api.mutations, [])
        self.assertEqual(expected["producer"]["workflow_path"], ".github/workflows/mod-base-build.yml")
        self.assertEqual(expected["producer"]["api_head_sha"], world.plan["identity"]["tested_sha"])

    def test_listing_is_keyed_by_the_default_branch_and_the_tested_commit_alone(self):
        world = build_world(push=True)
        with patch.object(world.api, "get_json", wraps=world.api.get_json) as get:
            self.call(world)
        listings = [call for call in get.call_args_list if call.args[0] == LISTING]
        self.assertEqual(len(listings), 1)
        params = listings[0].kwargs["params"]
        self.assertEqual((params["branch"], params["head_sha"]), ("master", "2" * 40))
        self.assertFalse({"event", "status"} & set(params))

    def test_runs_of_another_event_commit_branch_repository_or_workflow_are_not_candidates(self):
        for changes in ({"event": "schedule"}, {"event": "pull_request_target"}, {"event": "workflow_run"},
                        {"head_sha": "f" * 40}, {"head_branch": "other"},
                        {"head_repository": {"full_name": "fork/mod"}}, {"path": PACKAGED}):
            world = build_world(push=True)
            later_run(world, **changes)  # newer and failed: it would reject if it counted
            with self.subTest(changes=changes):
                self.assertEqual(self.call(world), world.bundle)
            world = World(push=True)
            world.add_run("build", "build-full", **changes)
            with self.subTest(changes=changes, only=True):
                self.assertIsNone(self.call(world))

    def test_no_run_and_a_newest_reuse_run_leave_nothing_to_select(self):
        world = World(push=True)
        self.assertIsNone(self.call(world))
        self.assertEqual(world.api.request_count, 4)  # the subject and the listing
        world = World(push=True)
        world.add_run("build", "build-reuse")
        self.assertIsNone(self.call(world))
        # A reuse run after an older full Build: the older bundle is never taken instead.
        world = build_world(push=True)
        later_run(world, listing="build-reuse", conclusion="success")
        self.assertIsNone(self.call(world))
        self.assertEqual(world.api.mutations, [])

    def test_a_pending_newest_run_is_a_rejection_not_a_reason_to_rebuild_or_look_back(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            for older in (False, True):
                world = build_world(push=True) if older else World(push=True)
                if older:
                    later_run(world, status=status, conclusion=None)
                else:
                    world.add_run("build", "build-full", status=status, conclusion=None)
                with self.subTest(status=status, older=older), self.assertRaisesRegex(MbError, "has not completed"):
                    self.call(world)
        world = World(push=True)
        world.add_run("build", "build-full", status="queued", conclusion="success")
        with self.assertRaisesRegex(MbError, "pending"):
            self.call(world)

    def test_failed_cancelled_and_other_unsuccessful_newest_runs_never_use_an_older_build(self):
        for conclusion in ("failure", "cancelled", "neutral", "skipped", "timed_out"):
            world = build_world(push=True)
            later_run(world, conclusion=conclusion)
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(MbError, "newest exact Build"):
                self.call(world)

    def test_any_other_graph_of_the_newest_run_is_a_rejection(self):
        for listing in ("build-deferred", "build-attest-only", "build-full-extra-job", "build-full-missing-job",
                        "build-full-duplicated-job", "build-full-wrong-conclusion", "packaged-reuse"):
            world = build_world(push=True)
            later_run(world, listing=listing, conclusion="success")
            with self.subTest(listing=listing), self.assertRaises(MbError):
                self.call(world)

    def test_a_job_of_another_attempt_makes_the_run_no_producer(self):
        world = build_world(push=True)
        world.job(42, ASSEMBLE)["run_attempt"] = 1
        world.set_jobs(42, world.jobs[42])
        with self.assertRaises(MbError):
            self.call(world)

    def test_missing_duplicate_expired_and_foreign_bundles_reject(self):
        for mutation in ("missing", "duplicate", "expired", "head"):
            world = build_world(push=True)
            if mutation == "missing":
                world.set_artifact(100, name=grammar.ci_artifact_name("build", 42, 1))
            elif mutation == "duplicate":
                world.api.add_artifact({**world.records[100], "id": 105}, b"duplicate")
            elif mutation == "expired":
                world.set_artifact(100, expired=True)
            else:
                world.set_artifact(100, workflow_run={"head_sha": "f" * 40})
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(world)

    def test_a_moved_default_branch_another_controller_or_another_kit_pin_rejects(self):
        world = build_world(push=True)
        world.api.set_branch("master", "f" * 40, "e" * 40)
        with self.assertRaisesRegex(MbError, "moved"):
            self.call(world)
        for index, message in ((0, "admitted controller commit"), (1, "pinned kit")):
            world = build_world(push=True)
            repin(world, 42, index)
            with self.subTest(index=index), self.assertRaisesRegex(MbError, message):
                self.call(world)

    def test_api_failure_is_never_absence(self):
        world = build_world(push=True)
        with patch.object(world.api, "get_json", side_effect=MbError("API unavailable")), \
                self.assertRaisesRegex(MbError, "API unavailable"):
            self.call(world)

    def test_a_pull_request_plan_is_refused_before_any_read(self):
        world = build_world()
        with patch.object(world.api, "get_json") as get, self.assertRaisesRegex(MbError, "waits for its own Build"):
            self.call(world)
        get.assert_not_called()

    def test_revalidation_accepts_only_the_unchanged_newest_descriptor(self):
        for change in (None, "pending", "failure", "reuse", "expired", "metadata", "descriptor", "controller"):
            world = build_world(push=True)
            if change == "pending":
                later_run(world, status="queued", conclusion=None)
            elif change == "failure":
                later_run(world)
            elif change == "reuse":
                later_run(world, listing="build-reuse", conclusion="success")
            elif change == "expired":
                world.set_artifact(100, expired=True)
            elif change == "metadata":
                world.set_artifact(100, digest="sha256:" + "f" * 64)
            elif change == "descriptor":
                world.bundle["artifact"]["id"] += 1
            elif change == "controller":
                world.api.set_branch("master", "f" * 40, "e" * 40)
            with self.subTest(change=change):
                if change is None:
                    self.assertIsNone(selection.revalidate_protected_build(world.api, descriptor=world.bundle,
                                                                          plan=world.plan))
                    self.assertEqual(world.api.request_count, 8)
                else:
                    with self.assertRaises(MbError):
                        selection.revalidate_protected_build(world.api, descriptor=world.bundle, plan=world.plan)
            self.assertEqual(world.api.mutations, [])


class ProtectedDownloadTests(unittest.TestCase):
    def call(self, world, output, **changes):
        return selection.download_protected_build(world.api, plan=world.plan, output=output, **changes)

    def test_the_newest_bundle_is_published_and_bound(self):
        world = build_world(push=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            self.assertEqual(self.call(world, output), {"descriptor": world.bundle, "envelope": world.envelope})
            published(self, world, output)
            self.assertEqual(list(Path(directory).iterdir()), [output])
        # Select 8, download 2, and before publication the subject 2, the listing, the run and the
        # bundle record once more.
        self.assertEqual(world.api.request_count, 15)
        self.assertEqual(world.api.mutations, [])

    def test_a_named_run_must_be_the_newest_build_run(self):
        world = build_world(push=True)
        with tempfile.TemporaryDirectory() as directory:
            observed = self.call(world, Path(directory) / "output", run_id=42)
            self.assertEqual(observed["descriptor"], world.bundle)
        for run_id in (41, 43, True, "42", 0):
            world = build_world(push=True)
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output", run_id=run_id)
            download.assert_not_called()
        # The named run exists, but a newer run of the same commit took its place.
        world = build_world(push=True)
        newer = build_world(push=True)
        world.api.add_run({**newer.runs[42], "id": 44, "created_at": "2026-10-07T11:00:00Z"})
        world.api.add_jobs(44, 2, [{**job, "run_id": 44} for job in newer.jobs[42]])
        world.api.add_artifact({**world.records[100], "id": 144, "name": grammar.ci_artifact_name("build", 44, 2),
                                "workflow_run": {**world.records[100]["workflow_run"], "id": 44}},
                               world.archives[100])
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "not the newest exact Build"):
            self.call(world, Path(directory) / "output", run_id=42)

    def test_nothing_to_select_is_none_only_when_no_run_was_named(self):
        for listing in (None, "build-reuse"):
            world = World(push=True)
            if listing is not None:
                world.add_run("build", listing)
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as directory:
                self.assertIsNone(self.call(world, Path(directory) / "output"))
                with self.assertRaisesRegex(MbError, "not the newest exact Build"):
                    self.call(world, Path(directory) / "output", run_id=42)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_supersession_expiry_or_a_moved_branch_during_download_publishes_nothing(self):
        changes = {"run": lambda w: later_run(w, status="queued", conclusion=None),
                   "attempt": lambda w: w.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "expired": lambda w: w.set_artifact(100, expired=True),
                   "branch": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40)}
        for name, change in changes.items():
            world = build_world(push=True)
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world)), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_output_and_a_pull_request_plan_reject_before_any_read(self):
        world = build_world(push=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as get:
            with self.assertRaises(MbError):
                self.call(world, Path(directory))
            with self.assertRaises(MbError):
                selection.download_protected_build(world.api, plan=build_world().plan,
                                                   output=Path(directory) / "output")
        get.assert_not_called()


class RebuiltBuildTests(unittest.TestCase):
    def call(self, world, output, **changes):
        arguments = {"plan": world.plan, "run_id": 43, "run_attempt": 2, "event": "push", "output": output, **changes}
        return selection.download_rebuilt_build(world.api, **arguments)

    def refused(self, world, message="", **changes):
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download:
            with self.assertRaisesRegex(MbError, message):
                self.call(world, Path(directory) / "output", **changes)
            download.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_running_run_reads_the_build_it_built_for_itself(self):
        for running in (True, False):
            world = rebuilt_world(running=running)
            with self.subTest(running=running), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                self.assertEqual(self.call(world, output), {"descriptor": world.bundle, "envelope": world.envelope})
                published(self, world, output)
                self.assertEqual(world.api.mutations, [])
        producer = world.bundle["producer"]
        self.assertEqual((producer["workflow_path"], producer["run_id"], producer["run_attempt"]), (PACKAGED, 43, 2))
        self.assertEqual(producer["graph_sha256"], run_graph("packaged", "rebuilt").sha256(world.plan))
        self.assertNotEqual(producer["graph_sha256"], run_graph("build", "full").sha256(world.plan))

    def test_the_reads_of_a_running_run_stay_within_a_small_budget(self):
        world = rebuilt_world()
        with tempfile.TemporaryDirectory() as directory:
            self.call(world, Path(directory) / "output")
        # The subject 3, the run, its jobs, the bundle listing and record, download 2; then the
        # subject 2, the run, the jobs with the bundle listing, and the bundle record once more.
        self.assertEqual(world.api.request_count, 15)

    def test_guard_selection_and_every_build_job_must_have_finished_and_sealed(self):
        def unsealed(job):
            job["steps"] = [step for step in job["steps"] if step["name"] != "Upload sealed outputs"]

        for name in (GUARD, SELECT, PLAN, POLICY, TARGET, ASSEMBLE, GATE):
            mutations = {"running": lambda job: job.update(status="in_progress", conclusion=None, completed_at=None),
                         "failed": lambda job: job.update(conclusion="failure"),
                         "skipped": lambda job: job.update(conclusion="skipped")}
            if name in (TARGET, ASSEMBLE, GATE):
                mutations["unsealed"] = unsealed
            for label, mutate in mutations.items():
                world = rebuilt_world()
                mutate(world.job(43, name))
                world.set_jobs(43, world.jobs[43])
                with self.subTest(job=name, mutation=label):
                    self.refused(world)
            world = rebuilt_world()
            world.set_jobs(43, [job for job in world.jobs[43] if job["name"] != name])
            with self.subTest(job=name, mutation="absent"):
                self.refused(world)

    def test_a_job_of_another_attempt_is_no_part_of_this_build(self):
        for name in (TARGET, ASSEMBLE, GATE):
            world = rebuilt_world()
            world.job(43, name)["run_attempt"] = 1
            world.set_jobs(43, world.jobs[43])
            with self.subTest(job=name):
                self.refused(world)

    def test_unexpected_and_duplicated_jobs_reject(self):
        world = rebuilt_world()
        world.set_jobs(43, [*world.jobs[43], {**world.job(43, TARGET), "id": 7}])
        self.refused(world)
        world = rebuilt_world()
        world.set_jobs(43, [*world.jobs[43], {**world.job(43, PLAN), "id": 7, "name": "Shared Build / Extra"}])
        self.refused(world)

    def test_the_run_must_be_this_live_attempt_of_the_packaged_caller_at_the_pinned_kit(self):
        cases = {"newer attempt": lambda run: run.update(run_attempt=3),
                 "failed": lambda run: run.update(status="completed", conclusion="failure"),
                 "queued": lambda run: run.update(status="queued"),
                 "event": lambda run: run.update(event="workflow_dispatch"),
                 "head": lambda run: run.update(head_sha="f" * 40),
                 "workflow": lambda run: run.update(path=CI_WORKFLOWS["build"]),
                 "unreferenced": lambda run: run.update(referenced_workflows=[])}
        for name, mutate in cases.items():
            world = rebuilt_world()
            mutate(world.runs[43])
            world.api.add_run(world.runs[43])
            with self.subTest(case=name):
                self.refused(world)
        for index, message in ((0, "admitted controller commit"), (1, "pinned kit")):
            world = rebuilt_world()
            repin(world, 43, index)
            with self.subTest(index=index):
                self.refused(world, message)
        self.refused(rebuilt_world(), run_id=42)
        self.refused(rebuilt_world(), run_attempt=1)

    def test_missing_duplicated_expired_and_foreign_bundles_reject(self):
        for mutation in ("missing", "duplicate", "expired", "head", "metadata"):
            world = rebuilt_world()
            if mutation == "missing":
                world.set_artifact(100, name=grammar.ci_artifact_name("build", 43, 1))
            elif mutation == "duplicate":
                world.api.add_artifact({**world.records[100], "id": 105}, b"duplicate")
            elif mutation == "expired":
                world.set_artifact(100, expired=True)
            elif mutation == "head":
                world.set_artifact(100, workflow_run={"head_sha": "f" * 40})
            else:
                world.set_artifact(100, created_at="2026-10-07T10:04:00Z")  # outside the upload window
            with self.subTest(mutation=mutation):
                self.refused(world)

    def test_a_selected_descriptor_must_still_be_the_bundle_of_this_run(self):
        world = rebuilt_world()
        with tempfile.TemporaryDirectory() as directory:
            observed = self.call(world, Path(directory) / "output", descriptor=world.bundle)
            self.assertEqual(observed["descriptor"], world.bundle)
        for change in ("digest", "window", "graph"):
            world = rebuilt_world()
            descriptor = copy.deepcopy(world.bundle)
            if change == "digest":
                descriptor["artifact"]["digest"] = "sha256:" + "f" * 64
            elif change == "window":
                descriptor["producer"]["upload_window"]["started_at"] = "2026-10-07T10:02:29Z"
            else:
                descriptor["producer"]["graph_sha256"] = "f" * 64
            with self.subTest(change=change):
                self.refused(world, "no longer the selected one", descriptor=descriptor)

    def test_movement_during_the_download_publishes_nothing(self):
        def reran(world):
            world.set_run(43, run_attempt=3)

        def unfinished(world):
            world.job(43, GATE).update(conclusion="failure")
            world.set_jobs(43, world.jobs[43])

        changes = {"attempt": reran, "jobs": unfinished,
                   "expired": lambda w: w.set_artifact(100, expired=True),
                   "branch": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40)}
        for name, change in changes.items():
            world = rebuilt_world()
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world)), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_pull_request_plan_and_malformed_arguments_reject_before_any_read(self):
        world = rebuilt_world()
        cases = [{"plan": build_world().plan}, {"run_id": 0}, {"run_id": True}, {"run_attempt": 0},
                 {"event": "pull_request_target"}, {"event": None}, {"descriptor": {}}]
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as get:
            for index, changes in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output", **changes)
            with self.assertRaises(MbError):
                self.call(world, Path(directory))
        get.assert_not_called()


class SelectBuildTests(unittest.TestCase):
    """``select_build``: the record each route writes, and every reason not to write one."""

    def select(self, world, parent, **changes):
        arguments = {"plan": world.plan, "run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED,
                     "event": "pull_request_target" if world.plan["identity"]["pr_number"] else "push",
                     "temporary_root": parent, "monotonic": lambda: 0,
                     "sleep": lambda _: self.fail("a finished Build must not sleep"), **changes}
        return selection.select_build(world.api, **arguments)

    def assert_record(self, world, record, *, requests):
        self.assertEqual(validate_source_selection(record, plan=world.plan), record)
        self.assertEqual(record["build"], world.bundle)
        self.assertEqual(record["envelope_sha256"], canonical_sha256(world.envelope))
        request = dict(record["request"])
        nonce = request.pop("nonce")
        self.assertRegex(nonce, "^[0-9a-f]{64}$")
        self.assertEqual(request, {"run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED,
                                   "workflow_ref": f"example/mod/{PACKAGED}@refs/heads/master"})
        self.assertEqual(world.api.request_count, requests)
        self.assertLess(world.api.request_count, 60)
        self.assertEqual(world.api.mutations, [])

    def test_a_pull_request_takes_the_newest_build_of_its_head_in_seventeen_requests(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory:
            record = self.select(world, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])  # the verified copy is not kept
        self.assert_record(world, record, requests=17)
        self.assertNotEqual(record["build"]["producer"]["run_id"], record["request"]["run_id"])

    def test_every_selection_has_a_nonce_of_its_own(self):
        nonces = set()
        for _ in range(3):
            world = build_world()
            with tempfile.TemporaryDirectory() as directory:
                nonces.add(self.select(world, Path(directory))["request"]["nonce"])
        self.assertEqual(len(nonces), 3)

    def test_one_pending_poll_costs_one_request(self):
        world = build_world()
        world.set_run(42, status="queued", conclusion=None)
        elapsed, counts = [0], []

        def sleep(seconds):
            counts.append(world.api.request_count)
            elapsed[0] += seconds
            if len(counts) == 4:
                world.set_run(42, status="completed", conclusion="success")

        with tempfile.TemporaryDirectory() as directory:
            record = self.select(world, Path(directory), monotonic=lambda: elapsed[0], sleep=sleep)
        # The first poll admits the pull request (3 and its commit) and lists; every later one lists.
        self.assertEqual(counts, [5, 6, 7, 8])
        self.assertEqual(elapsed[0], 4 * limits.CI_BUILD_POLL_SECONDS)
        self.assertEqual(record["build"], world.bundle)
        # A whole wait of 91 polls and the selection that ends it fit the command's budget.
        self.assertLessEqual(4 + limits.MAX_CI_BUILD_POLLS + 16, limits.MAX_CI_SELECT_BUILD_REQUESTS)

    def test_no_build_a_pending_build_and_a_deferral_wait_to_the_deadline_then_fail_closed(self):
        def absent():
            return World()

        def pending():
            world = build_world()
            world.set_run(42, status="in_progress", conclusion=None)
            return world

        def deferred():
            world = World()
            world.add_run("build", "build-deferred")
            return world

        def deferred_after_success():
            world = build_world()
            later_run(world, listing="build-deferred", conclusion="success")
            return world

        for build in (absent, pending, deferred, deferred_after_success):
            for wait_seconds, sleeps in ((5400, [60] * 90), (150, [60, 60, 30])):
                world, elapsed, slept = build(), [0], []

                def sleep(seconds):
                    slept.append(seconds)
                    elapsed[0] += seconds

                with self.subTest(world=build.__name__, wait_seconds=wait_seconds), \
                        tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download:
                    with self.assertRaisesRegex(MbError, f"exhausted the {wait_seconds}-second.*rerun complete Build"):
                        self.select(world, Path(directory), wait_seconds=wait_seconds,
                                    monotonic=lambda: elapsed[0], sleep=sleep)
                    download.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(slept, sleeps)

    def test_the_wait_can_be_shortened_but_never_extended(self):
        world = build_world()
        for wait_seconds in (0, -1, 5401, True, 60.0, "60", None):
            with self.subTest(wait_seconds=wait_seconds), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as get, self.assertRaises(MbError):
                self.select(world, Path(directory), wait_seconds=wait_seconds)
            get.assert_not_called()

    def test_a_failed_or_cancelled_newest_build_rejects_at_once_and_never_uses_an_older_one(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            world = build_world()
            later_run(world, conclusion=conclusion)
            sleep = Mock()
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download:
                with self.assertRaisesRegex(MbError, "newest exact Build failed or was cancelled"):
                    self.select(world, Path(directory), sleep=sleep)
                download.assert_not_called()
            sleep.assert_not_called()

    def test_a_moved_tuple_base_or_merge_and_a_draft_reject(self):
        def moved_head(world):
            world.api.add_response("/repos/example/mod/pulls/7", {**world.pr, "head": {**world.pr["head"],
                                                                                        "sha": "f" * 40}})

        cases = {"base": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40), "head": moved_head,
                 "merge": lambda w: w.api.add_response("/repos/example/mod/pulls/7",
                                                       {**w.pr, "merge_commit_sha": "f" * 40}),
                 "unmergeable": lambda w: w.api.add_response("/repos/example/mod/pulls/7",
                                                             {**w.pr, "merge_commit_sha": None}),
                 "draft": lambda w: w.api.add_response("/repos/example/mod/pulls/7", {**w.pr, "draft": True}),
                 "closed": lambda w: w.api.add_response("/repos/example/mod/pulls/7", {**w.pr, "state": "closed"})}
        for name, change in cases.items():
            world = build_world()
            change(world)
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.select(world, Path(directory), sleep=Mock())
            download.assert_not_called()

    def test_a_missing_or_expired_bundle_and_malformed_metadata_are_fatal(self):
        def other_digest(world):
            world.set_artifact(100, digest="sha256:" + "f" * 64)

        def corrupt(world):
            data = bytearray(world.archives[100])
            data[len(data) // 2] ^= 0xFF
            world.api.add_artifact(world.records[100], bytes(data))

        def unsealed(world):
            job = world.job(42, ASSEMBLE)
            job["steps"] = [step for step in job["steps"] if step["name"] != "Validate frozen native exports"]
            world.set_jobs(42, world.jobs[42])

        cases = {"missing": lambda w: w.set_artifact(100, name=grammar.ci_artifact_name("build", 42, 1)),
                 "expired": lambda w: w.set_artifact(100, expired=True),
                 "duplicate": lambda w: w.api.add_artifact({**w.records[100], "id": 105}, b"duplicate"),
                 "digest": other_digest, "bytes": corrupt, "unsealed": unsealed,
                 "graph": lambda w: w.set_jobs(42, ci_graph_jobs("build-full-extra-job")),
                 "controller": lambda w: repin(w, 42, 0), "kit": lambda w: repin(w, 42, 1),
                 "created_at": lambda w: w.set_run(42, created_at="yesterday")}
        for name, change in cases.items():
            world = build_world()
            change(world)
            sleep = Mock()
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(MbError):
                    self.select(world, Path(directory), sleep=sleep)
                self.assertEqual(list(Path(directory).iterdir()), [])
            sleep.assert_not_called()

    def test_a_rerun_that_mixes_attempts_is_no_producer(self):
        world = build_world()
        world.job(42, ASSEMBLE)["run_attempt"] = 1  # only the failed jobs of attempt 1 ran again
        world.set_jobs(42, world.jobs[42])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.select(world, Path(directory), sleep=Mock())
        world = build_world()
        world.set_run(42, run_attempt=3)  # attempt 3 has no jobs: nothing of attempt 2 is reused
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.select(world, Path(directory), sleep=Mock())

    def test_an_api_failure_is_never_absence(self):
        world = build_world()
        failure = ApiError("GitHub API GET failed: HTTP 502", status=502, method="GET", path="/")
        sleep = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json", side_effect=failure), \
                self.assertRaisesRegex(ApiError, "HTTP 502"):
            self.select(world, Path(directory), sleep=sleep)
        sleep.assert_not_called()

    def test_a_pull_request_names_no_build_run(self):
        world = build_world()
        for build_run_id in (42, selection.SAME_RUN):
            with self.subTest(build_run_id=build_run_id), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as get, self.assertRaisesRegex(MbError, "names none"):
                self.select(world, Path(directory), build_run_id=build_run_id)
            get.assert_not_called()

    def test_a_protected_subject_selects_the_newest_build_of_its_commit(self):
        for build in (lambda: build_world(push=True), dispatched_world):
            world = build()
            with self.subTest(event=world.runs[42]["event"]), tempfile.TemporaryDirectory() as directory:
                record = self.select(world, Path(directory), sleep=Mock(side_effect=AssertionError))
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assert_record(world, record, requests=15)
            self.assertEqual(record["build"]["producer"]["event"], world.runs[42]["event"])

    def test_a_protected_subject_without_a_build_gets_no_record_and_never_waits(self):
        for listing in (None, "build-reuse"):
            world = World(push=True)
            if listing is not None:
                world.add_run("build", listing)
            sleep = Mock()
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as directory:
                self.assertIsNone(self.select(world, Path(directory), sleep=sleep))
                self.assertEqual(list(Path(directory).iterdir()), [])
            sleep.assert_not_called()
            # The subject (3), the listing and, for a finished reuse run, the run and its jobs.
            self.assertEqual(world.api.request_count, 4 if listing is None else 6)

    def test_a_protected_subject_waits_for_its_sibling_build_one_request_per_poll(self):
        # A push starts the Build caller and the packaged caller together: the packaged run
        # always finds the Build run of its commit still running.
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            world = build_world(push=True)
            world.set_run(42, status=status, conclusion=None)
            elapsed, counts = [0], []

            def sleep(seconds):
                counts.append(world.api.request_count)
                elapsed[0] += seconds
                if len(counts) == 4:
                    world.set_run(42, status="completed", conclusion="success")

            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                record = self.select(world, Path(directory), monotonic=lambda: elapsed[0], sleep=sleep)
                self.assertEqual(list(Path(directory).iterdir()), [])
                # The first poll admits the subject (3) and lists; every later one lists.
                self.assertEqual(counts, [4, 5, 6, 7])
                self.assertEqual(elapsed[0], 4 * limits.CI_BUILD_POLL_SECONDS)
                # The poll that finds the run complete lists and describes it (5), the subject is
                # admitted again (its branch, 2: the commit object never changes), and the
                # download with its last observation costs the 7 of an immediate selection.
                self.assert_record(world, record, requests=7 + 5 + 2 + 7)
        self.assertLessEqual(3 + limits.MAX_CI_BUILD_POLLS + 15, limits.MAX_CI_SELECT_BUILD_REQUESTS)

    def test_a_protected_wait_ends_at_the_deadline_and_fails_closed(self):
        for wait_seconds, sleeps in ((5400, [60] * 90), (150, [60, 60, 30])):
            for older in (False, True):
                world = build_world(push=True) if older else World(push=True)
                if older:
                    later_run(world, status="in_progress", conclusion=None)
                else:
                    world.add_run("build", "build-full", status="in_progress", conclusion=None)
                elapsed, slept = [0], []

                def sleep(seconds):
                    slept.append(seconds)
                    elapsed[0] += seconds

                with self.subTest(wait_seconds=wait_seconds, older=older), \
                        tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download:
                    with self.assertRaisesRegex(MbError, f"exhausted the {wait_seconds}-second"):
                        self.select(world, Path(directory), wait_seconds=wait_seconds,
                                    monotonic=lambda: elapsed[0], sleep=sleep)
                    download.assert_not_called()  # the finished older Build is never the fallback
                    self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(slept, sleeps)
                self.assertEqual(world.api.request_count, 3 + len(sleeps))

    def test_a_sibling_build_that_fails_while_waited_for_is_a_rejection_at_once(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            world = build_world(push=True)
            world.set_run(42, status="in_progress", conclusion=None)
            slept = []

            def sleep(seconds):
                slept.append(seconds)
                world.set_run(42, status="completed", conclusion=conclusion)

            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download:
                with self.assertRaisesRegex(MbError, "newest exact Build failed or was cancelled"):
                    self.select(world, Path(directory), monotonic=lambda: len(slept), sleep=sleep)
                download.assert_not_called()
            self.assertEqual(slept, [60])
        # A failed newest run is a rejection without any wait, whatever an older run built.
        world = build_world(push=True)
        later_run(world, conclusion="failure")
        sleep = Mock()
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "failed or was cancelled"):
            self.select(world, Path(directory), sleep=sleep)
        sleep.assert_not_called()

    def test_a_sibling_build_that_ends_as_an_admitted_reuse_leaves_nothing_to_select(self):
        world = World(push=True)
        world.add_run("build", "build-reuse", status="in_progress", conclusion=None)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            world.set_run(42, status="completed", conclusion="success")

        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self.select(world, Path(directory), monotonic=lambda: len(slept), sleep=sleep))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(slept, [60])

    def test_a_default_branch_that_moves_during_the_wait_is_a_rejection(self):
        world = build_world(push=True)
        world.set_run(42, status="in_progress", conclusion=None)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            world.api.set_branch("master", "f" * 40, "e" * 40)
            world.set_run(42, status="completed", conclusion="success")

        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download, \
                self.assertRaises(MbError):
            self.select(world, Path(directory), monotonic=lambda: len(slept), sleep=sleep)
        download.assert_not_called()
        self.assertEqual(slept, [60])

    def test_a_named_build_run_is_authenticated_exactly(self):
        world = build_world(push=True)
        with tempfile.TemporaryDirectory() as directory:
            record = self.select(world, Path(directory), build_run_id=42)
        self.assert_record(world, record, requests=15)
        for build_run_id in (41, "42", "latest"):
            world = build_world(push=True)
            with self.subTest(build_run_id=build_run_id), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaises(MbError):
                self.select(world, Path(directory), build_run_id=build_run_id)

    def test_same_run_selects_the_build_this_run_built(self):
        world = rebuilt_world()
        with tempfile.TemporaryDirectory() as directory:
            record = self.select(world, Path(directory), build_run_id=selection.SAME_RUN)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assert_record(world, record, requests=15)
        producer, request = record["build"]["producer"], record["request"]
        self.assertEqual((producer["run_id"], producer["run_attempt"], producer["workflow_path"]),
                         (request["run_id"], request["run_attempt"], request["workflow_path"]))
        # Another run or attempt of the packaged caller is not "this run".
        for changes in ({"run_id": 44}, {"run_attempt": 1}, {"run_attempt": 3}):
            world = rebuilt_world()
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaises(MbError):
                self.select(world, Path(directory), build_run_id=selection.SAME_RUN, **changes)

    def test_only_a_run_of_the_packaged_caller_selects(self):
        world = build_world()
        for workflow_path in (CI_WORKFLOWS["build"], ".github/workflows/mod-base-gate-status.yml", "", None):
            with self.subTest(workflow_path=workflow_path), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as get, self.assertRaises(MbError):
                self.select(world, Path(directory), workflow_path=workflow_path)
            get.assert_not_called()
        for changes in ({"run_id": 0}, {"run_attempt": True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as get, self.assertRaises(MbError):
                self.select(world, Path(directory), **changes)
            get.assert_not_called()

    def test_a_missing_private_directory_is_a_rejection(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "private Build copy"):
            self.select(world, Path(directory) / "absent")


class FetchBuildTests(unittest.TestCase):
    """``fetch_build`` trusts the protected handover and verifies the exact selected bytes."""

    def selected(self, world, **changes):
        arguments = {"plan": world.plan, "run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED,
                     "event": "pull_request_target" if world.plan["identity"]["pr_number"] else "push", **changes}
        with tempfile.TemporaryDirectory() as directory:
            record = selection.select_build(world.api, temporary_root=Path(directory), monotonic=lambda: 0,
                                            sleep=Mock(side_effect=AssertionError), **arguments)
        self.spent = world.api.request_count
        return record

    def fetch(self, world, record, output, **changes):
        arguments = {"record": record, "plan": world.plan, "run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED,
                     "event": "pull_request_target" if world.plan["identity"]["pr_number"] else "push",
                     "output": output, **changes}
        return selection.fetch_build(world.api, **arguments)

    def refused(self, world, record, message="", **changes):
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download:
            with self.assertRaisesRegex(MbError, message):
                self.fetch(world, record, Path(directory) / "sealed-build", **changes)
            download.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_each_route_publishes_the_selected_bundle_within_budget(self):
        # One REST request returns the archive URL; one credential-free storage GET reads it.
        cases = {"pull request": (build_world, {}, 2), "selected": (lambda: build_world(push=True), {}, 2),
                 "rebuilt": (rebuilt_world, {"build_run_id": selection.SAME_RUN}, 2)}
        for name, (build, changes, requests) in cases.items():
            world = build()
            record = self.selected(world, **changes)
            with self.subTest(route=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "sealed-build"
                self.assertEqual(self.fetch(world, record, output), world.envelope)
                published(self, world, output)
                self.assertEqual(list(Path(directory).iterdir()), [output])
                self.assertEqual(world.api.request_count - self.spent, requests)
                self.assertEqual(world.api.mutations, [])

    def test_an_expired_download_rejects_without_publication(self):
        for build, changes in ((build_world, {}), (lambda: build_world(push=True), {}),
                               (rebuilt_world, {"build_run_id": selection.SAME_RUN})):
            world = build()
            record = self.selected(world, **changes)
            world.set_artifact(100, expired=True)
            with self.subTest(route=build.__name__), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ApiError, "expired"):
                    self.fetch(world, record, Path(directory) / "sealed-build")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_freshness_is_left_to_the_gate_after_selection(self):
        # The packaged gate tests require newest Build and live source again before sealing.
        # Every matrix lane reads only the immutable bytes named by its protected input job.
        for push in (False, True):
            world = build_world(push=push)
            record = self.selected(world)
            later_run(world, status="queued", conclusion=None)
            world.api.set_branch("master", "f" * 40, "e" * 40)
            with self.subTest(push=push), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(self.fetch(world, record, Path(directory) / "sealed-build"), world.envelope)
                self.assertEqual(world.api.request_count - self.spent, 2)

    def test_only_the_run_attempt_that_selected_may_consume_the_selection(self):
        world = build_world()
        record = self.selected(world)
        for changes in ({"run_attempt": 3}, {"run_attempt": 1}, {"run_id": 44},
                        {"workflow_path": CI_WORKFLOWS["build"]}):
            with self.subTest(changes=changes), patch.object(world.api, "get_json") as get:
                self.refused(world, record, **changes)
            get.assert_not_called()
        # A bundle of the Build caller never comes from the consuming run itself.
        own = copy.deepcopy(record)
        own["request"].update(run_id=42)
        with patch.object(world.api, "get_json") as get:
            self.refused(world, own, "comes from a separate run", run_id=42)
        get.assert_not_called()
        # A rebuilt Build serves the run attempt that built it and no other.
        world = rebuilt_world()
        record = self.selected(world, build_run_id=selection.SAME_RUN)
        foreign = copy.deepcopy(record)
        foreign["request"].update(run_id=44)
        with patch.object(world.api, "get_json") as get:
            self.refused(world, foreign, "rebuilt Build serves only", run_id=44)
        get.assert_not_called()

    def test_a_record_of_another_plan_kind_or_shape_rejects_before_any_read(self):
        world = build_world()
        record = self.selected(world)
        other = build_world(push=True)
        cases = [{"plan": other.plan}, {"record": {}}, {"record": None}, {"record": {**record, "extra": 1}},
                 {"record": {**record, "plan_sha256": "f" * 64}},
                 {"record": {**record, "request": {**record["request"], "nonce": "short"}}},
                 {"record": {**record, "build": {**record["build"], "artifact": {
                     **record["build"]["artifact"], "name": grammar.ci_artifact_name("target", 42, 2, "target-a")}}}}]
        for index, changes in enumerate(cases):
            with self.subTest(index=index), patch.object(world.api, "get_json") as get:
                self.refused(world, changes.get("record", record), **{key: value for key, value in changes.items()
                                                                     if key != "record"})
            get.assert_not_called()

    def test_a_changed_descriptor_or_envelope_hash_is_not_the_selected_build(self):
        world = build_world()
        record = self.selected(world)
        changed = copy.deepcopy(record)
        changed["build"]["artifact"]["id"] += 1
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.fetch(world, changed, Path(directory) / "sealed-build")
        changed = {**record, "envelope_sha256": "f" * 64}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "envelope differs"):
            self.fetch(world, changed, Path(directory) / "sealed-build")

    def test_an_existing_output_is_never_replaced(self):
        for build, changes in ((build_world, {}), (rebuilt_world, {"build_run_id": selection.SAME_RUN})):
            world = build()
            record = self.selected(world, **changes)
            with self.subTest(route=build.__name__), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download:
                (Path(directory) / "kept").write_bytes(b"kept")
                with self.assertRaises(MbError):
                    self.fetch(world, record, Path(directory))
                download.assert_not_called()
                self.assertEqual([path.name for path in Path(directory).iterdir()], ["kept"])

    def test_an_api_failure_is_never_permission_to_use_the_selection(self):
        world = build_world()
        record = self.selected(world)
        failure = ApiError("GitHub API GET failed: HTTP 502", status=502, method="GET", path="/")
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download", side_effect=failure), \
                self.assertRaisesRegex(ApiError, "HTTP 502"):
            self.fetch(world, record, Path(directory) / "sealed-build")


if __name__ == "__main__":
    unittest.main()
