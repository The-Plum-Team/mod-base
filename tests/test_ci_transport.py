"""Numeric-ID transport against a fake GitHub that answers as the REST API does.

Only the API is faked. Archives are real ZIPs, extraction, verification and atomic publication
run on the real filesystem (Linux), and job listings are the literal ones of
``tests/fixtures/ci_graphs``. Runs and artifacts carry the head GitHub records them under.
"""

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.reads import CommandReads
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from tests.ci_attempt import sealed, validation
from tests.helpers import (ci_api_artifact, ci_api_run, ci_graph_jobs, ci_run_descriptor, ci_run_gate,
                           ci_runtime_envelope)
from tests.test_ci_protocol import protected_subject, seeded_pr

#: Upload windows and creation times of the literal listings, as a descriptor stores them.
WINDOWS = {
    "target": ("2026-10-07T10:01:00Z", "2026-10-07T10:02:00Z", "2026-10-07T10:01:30Z"),
    "build": ("2026-10-07T10:02:30Z", "2026-10-07T10:03:30Z", "2026-10-07T10:03:00Z"),
    "tested-build": ("2026-10-07T10:05:00Z", "2026-10-07T10:06:00Z", "2026-10-07T10:05:30Z"),
    "runtime": ("2026-10-07T10:08:00Z", "2026-10-07T10:09:00Z", "2026-10-07T10:08:30Z"),
    "results": ("2026-10-07T10:09:30Z", "2026-10-07T10:10:30Z", "2026-10-07T10:10:00Z"),
    "tested-packaged": ("2026-10-07T10:12:00Z", "2026-10-07T10:13:00Z", "2026-10-07T10:12:30Z"),
}
IDS = {"build": 100, "runtime": 101, "results": 102, "tested-build": 200, "tested-packaged": 201}
ASSEMBLE = "Shared Build / Seal complete Build bundle"
GATE = "Shared Build / Verify complete Build"


def build_archive(plan, producer, *, target_id=None):
    """A real export ZIP for a producer record: every planned file and the canonical envelope."""

    outputs = sorted((output for target in plan["targets"] if target_id in (None, target["id"])
                      for output in target["outputs"]), key=lambda output: output["path"])
    files = []
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for output in outputs:
            data = (output["path"] + "\n").encode()
            files.append({**output, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            archive.writestr(output["path"], data)
        envelope = {"kind": "mod-base.build.envelope", "schema_version": 1,
                    "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                    "profile": plan["profile"],
                    "producer": {key: value for key, value in producer.items() if key != "upload_window"},
                    "scope": "complete" if target_id is None else "target", "target_id": target_id, "files": files,
                    "native_reports": [file["path"] for file in files if file["role"] == "native-report"]}
        archive.writestr(grammar.CI_ENVELOPE_NAME, canonical_json(envelope))
    return stream.getvalue(), envelope


def sealed_build_archive(plan, producer, *, source_config_sha256=None):
    """The complete Build archive the assembling job uploads, including its validation."""

    data, envelope = build_archive(plan, producer)
    changes = {} if source_config_sha256 is None else {"source_config_sha256": source_config_sha256}
    record, reports = validation(plan, hook="verify_build", unit_id=None, run_id=producer["run_id"],
                                 run_attempt=producer["run_attempt"], input_sha256=canonical_sha256(envelope), **changes)
    return sealed(data, record, reports), envelope


def runtime_archive(plan, producer, owning_build):
    """A real complete results ZIP: one report, one empty log and the canonical runtime envelope."""

    contents = {"lanes/lane-a/result.json": b'{"opaque":"authored fixture"}\n', "lanes/lane-a/runtime.log": b""}
    envelope = ci_runtime_envelope()
    envelope.update(identity=copy.deepcopy(plan["identity"]), plan_sha256=plan["plan_sha256"], profile=plan["profile"],
                    producer={key: value for key, value in producer.items() if key != "upload_window"},
                    owning_build=copy.deepcopy(owning_build))
    envelope["files"] = [{"path": name, "lane_id": "lane-a",
                          "role": "runtime-log" if name.endswith(".log") else "native-report",
                          "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                         for name, data in sorted(contents.items())]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr(grammar.CI_RUNTIME_ENVELOPE_NAME, canonical_json(envelope))
    return stream.getvalue(), envelope


def record_archive(document, filename=grammar.CI_GATE_NAME, raw=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(filename, canonical_json(document) if raw is None else raw)
    return stream.getvalue()


class World:
    """One generation on a fake GitHub: its source, runs, literal job listings and real archives."""

    def __init__(self, *, push=False):
        self.plan, self.api, self.pr = seeded_pr()
        if push:
            protected_subject(self.plan, self.api, current=True)
        self.runs, self.jobs, self.records, self.archives = {}, {}, {}, {}

    def add_run(self, producer, listing, **changes):
        run = ci_api_run(self.plan, producer, **changes)
        self.runs[run["id"]] = run
        self.api.add_run(run)
        self.set_jobs(run["id"], ci_graph_jobs(listing))
        return run

    def set_run(self, run_id, **changes):
        self.runs[run_id].update(changes)
        self.api.add_run(self.runs[run_id])

    def set_jobs(self, run_id, jobs):
        self.jobs[run_id] = jobs
        self.api.add_jobs(run_id, 2, jobs)

    def job(self, run_id, name):
        return next(job for job in self.jobs[run_id] if job["name"] == name)

    def describe(self, producer, mode, key, data, *, unit_id=None, artifact_id=None):
        kind = key.split("-")[0]
        unit_id = key.split("-")[1] if kind == "tested" else unit_id
        descriptor = ci_run_descriptor(self.plan, producer, mode, kind, unit_id=unit_id,
                                       artifact_id=artifact_id or IDS[key])
        started, completed, created = WINDOWS[key]
        descriptor["producer"]["upload_window"] = {"started_at": started, "completed_at": completed}
        descriptor["artifact"].update(created_at=created, size=len(data),
                                      digest="sha256:" + hashlib.sha256(data).hexdigest())
        return descriptor

    def publish(self, descriptor, data, **changes):
        record = ci_api_artifact(descriptor, **changes)
        self.records[record["id"]], self.archives[record["id"]] = record, data
        self.api.add_artifact(record, data)
        return descriptor

    def set_artifact(self, artifact_id, **changes):
        workflow_run = changes.pop("workflow_run", {})
        self.records[artifact_id].update(changes)
        self.records[artifact_id]["workflow_run"].update(workflow_run)
        self.api.add_artifact(self.records[artifact_id], self.archives[artifact_id])

    def add_bundle(self, producer="build", mode="full"):
        """The complete Build bundle of the run of ``producer`` (a packaged run only when it rebuilt)."""

        record = ci_run_descriptor(self.plan, producer, mode, "build")["producer"]
        data, self.envelope = sealed_build_archive(self.plan, record,
                                                   source_config_sha256=getattr(self, "config_sha256", None))
        self.bundle = self.publish(self.describe(producer, mode, "build", data), data)
        return self.bundle


def build_world(*, push=False, **run):
    """A subject and its finished full Build run with a real complete bundle."""

    world = World(push=push)
    world.add_run("build", "build-full", **run)
    world.add_bundle()
    return world


def transport_fixture():
    """``(plan, api, descriptor, envelope, run, record, archive)`` of a finished pull-request Build."""

    world = build_world()
    return (world.plan, world.api, world.bundle, world.envelope, world.runs[42], world.records[100],
            world.archives[100])


def two_target_plan(plan):
    target, lane = copy.deepcopy(plan["targets"][0]), copy.deepcopy(plan["lanes"][0])
    target["id"] = "target-b"
    for output in target["outputs"]:
        output.update(path=output["path"].replace("lane-a", "lane-b"), lane_id="lane-b")
    lane.update(id="lane-b", target_id="target-b")
    plan["targets"].append(target)
    plan["lanes"].append(lane)
    plan["plan_sha256"] = plan_sha256(plan)


def target_set_world():
    """A two-target Build still running: both targets are sealed, the assembling job is the reader."""

    world = World()
    two_target_plan(world.plan)
    world.add_run("build", "build-full", status="in_progress", conclusion=None)
    jobs = [job for job in world.jobs[42] if job["name"] != GATE]
    assemble = next(job for job in jobs if job["name"] == ASSEMBLE)
    assemble.update(status="in_progress", conclusion=None, completed_at=None)
    assemble["steps"] = assemble["steps"][:1]
    second = copy.deepcopy(next(job for job in jobs if job["name"] == "Shared Build / Compile target target-a"))
    second.update(id=second["id"] + 7, name="Shared Build / Compile target target-b")
    jobs.insert(-1, second)
    world.set_jobs(42, jobs)
    world.descriptors, world.partitions = [], []
    for index, target in enumerate(world.plan["targets"]):
        record = ci_run_descriptor(world.plan, "build", "full", "target", unit_id=target["id"])["producer"]
        data, envelope = build_archive(world.plan, record, target_id=target["id"])
        descriptor = world.publish(world.describe("build", "full", "target", data, unit_id=target["id"],
                                                  artifact_id=110 + index), data)
        world.descriptors.append(descriptor)
        world.partitions.append({"descriptor": descriptor, "envelope": envelope})
    return world


@contextmanager
def after_download(api, action, *, count=1):
    """Run ``action`` right after the ``count``-th artifact download: the API moving mid-command."""

    original, seen = api.download, [0]

    def download(path, **kwargs):
        data = original(path, **kwargs)
        seen[0] += 1
        if seen[0] == count:
            action()
        return data

    with patch.object(api, "download", side_effect=download) as downloads:
        yield downloads


def exported(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


class CompletedBuildTransportTests(unittest.TestCase):
    def call(self, world, output, **changes):
        arguments = {"descriptor": world.bundle, "plan": world.plan, "output": output, **changes}
        return transport.download_completed_build(world.api, **arguments)

    def test_real_download_extraction_verification_and_private_copy(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            self.assertEqual(self.call(world, output), world.envelope)
            self.assertEqual(verify_build_export(output, plan=world.plan), world.envelope)
            self.assertEqual(exported(output), sorted([grammar.CI_ENVELOPE_NAME,
                                                       *(file["path"] for file in world.envelope["files"])]))
            self.assertEqual(list(Path(directory).iterdir()), [output])
        self.assertEqual(world.api.mutations, [])
        self.assertNotIn("upload_window", world.envelope["producer"])

    def test_a_bare_complete_export_cannot_be_downloaded(self):
        world = build_world()
        data, _ = build_archive(world.plan, world.bundle["producer"])
        world.bundle = world.publish(world.describe("build", "full", "build", data), data)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with self.assertRaises(MbError):
                self.call(world, output)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_request_budget_and_single_reads_of_immutable_objects(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "get_json", wraps=world.api.get_json) as reads:
            self.call(world, Path(directory) / "output")
        paths = [call.args[0] for call in reads.call_args_list]
        # Source, run and artifact twice (start, before publication); commit and jobs once; one download.
        self.assertEqual(world.api.request_count, 14)
        self.assertEqual(sum("/git/commits/" in path for path in paths), 1)
        self.assertEqual(sum(path.endswith("/attempts/2/jobs") for path in paths), 1)
        self.assertEqual(sum(path.endswith("/actions/runs/42") for path in paths), 2)
        self.assertEqual(sum(path.endswith("/pulls/7") for path in paths), 2)
        self.assertEqual(sum(path.endswith("/actions/artifacts/100") for path in paths), 2)

    def test_run_is_authenticated_under_the_pull_request_head_not_the_controller(self):
        world = build_world()
        identity = world.plan["identity"]
        self.assertEqual((world.runs[42]["head_sha"], world.runs[42]["head_branch"]),
                         (identity["head_sha"], identity["head_branch"]))
        self.assertEqual(world.records[100]["workflow_run"]["head_sha"], identity["head_sha"])
        # The shape the kit used to expect: a run recorded under the base branch and controller commit.
        for changes in ({"head_sha": identity["controller_sha"]}, {"head_branch": identity["base_branch"]},
                        {"head_sha": identity["tested_sha"]},
                        {"head_repository": {"full_name": "fork/mod"}}):
            world = build_world(**changes)
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, \
                    self.assertRaisesRegex(MbError, "not recorded under this subject"):
                self.call(world, Path(directory) / "output")
            download.assert_not_called()

    def test_wrong_attempt_event_path_conclusion_controller_and_kit_reject_before_download(self):
        def controller(run):
            entry = run["referenced_workflows"][0]
            entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)

        mutations = [lambda r: r.update(run_attempt=3), lambda r: r.update(event="pull_request"),
                     lambda r: r.update(conclusion="failure"), lambda r: r.update(status="in_progress", conclusion=None),
                     lambda r: r.update(referenced_workflows=[]), controller,
                     lambda r: r["referenced_workflows"][1].update(sha="9" * 40),
                     lambda r: r["referenced_workflows"].pop(1),
                     lambda r: r.update(path=".github/workflows/mod-base-packaged-e2e.yml")]
        for index, mutate in enumerate(mutations):
            world = build_world()
            mutate(world.runs[42])
            world.api.add_run(world.runs[42])
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            download.assert_not_called()

    def test_artifact_metadata_owner_head_and_expiry_reject_before_download(self):
        mutations = [{"expired": True}, {"name": "mb-ci-build--43--a2"}, {"digest": "sha256:" + "f" * 64},
                     {"size_in_bytes": 1}, {"created_at": "2026-10-07T10:03:01Z"},
                     {"expires_at": "2026-10-09T00:00:00Z"}, {"workflow_run": {"id": 43}},
                     {"workflow_run": {"head_sha": "2" * 40}}, {"workflow_run": {"head_branch": "master"}}]
        for changes in mutations:
            world = build_world()
            world.set_artifact(100, **copy.deepcopy(changes))
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            download.assert_not_called()

    def test_api_times_with_fractions_and_offsets_equal_the_stored_whole_second_form(self):
        world = build_world()
        world.set_artifact(100, created_at="2026-10-07T12:03:00.917+02:00", expires_at="2026-10-14T10:01:30.000Z")
        for step in world.job(42, ASSEMBLE)["steps"]:
            for key in ("started_at", "completed_at"):
                self.assertTrue(step[key].endswith(".000Z"))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.call(world, Path(directory) / "output"), world.envelope)
        world = build_world()
        world.set_artifact(100, created_at="2026-10-07T10:03:01.000Z")
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "created_at"):
            self.call(world, Path(directory) / "output")

    def test_graph_and_upload_window_are_mandatory(self):
        for change in ("graph", "window", "jobs", "seal"):
            world = build_world()
            if change == "graph":
                world.bundle["producer"]["graph_sha256"] = "f" * 64
            elif change == "window":
                world.bundle["producer"]["upload_window"]["started_at"] = "2026-10-07T10:02:29Z"
            elif change == "jobs":
                world.set_jobs(42, ci_graph_jobs("build-full-extra-job"))
            else:
                world.job(42, ASSEMBLE)["steps"][2]["conclusion"] = "failure"
                world.set_jobs(42, world.jobs[42])
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            download.assert_not_called()

    def test_deferred_attest_only_and_reuse_shaped_runs_are_not_a_complete_build(self):
        for listing in ("build-deferred", "build-reuse", "build-attest-only", "build-full-missing-job"):
            world = build_world()
            world.set_jobs(42, ci_graph_jobs(listing))
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaisesRegex(MbError, "exact job graph mismatch"):
                self.call(world, Path(directory) / "output")

    def test_corrupt_archive_and_wrong_envelope_never_publish(self):
        world = build_world()
        other = copy.deepcopy(world.bundle["producer"])
        other["run_id"] = 44
        cases = {"bytes": b"x" * len(world.archives[100]), "short": b"bad",
                 "producer": build_archive(world.plan, other)[0],
                 "scope": build_archive(world.plan, world.bundle["producer"], target_id="target-a")[0]}
        for name, data in cases.items():
            world = build_world()
            if name in ("producer", "scope"):  # a self-consistent archive of something else
                world.bundle["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
                world.archives[100] = data
                world.set_artifact(100, size_in_bytes=len(data), digest=world.bundle["artifact"]["digest"])
            else:
                world.api.add_artifact(world.records[100], data)
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_run_and_artifact_movement_after_download_forbid_publication(self):
        changes = {"source": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40),
                   "draft": lambda w: w.api.add_response("/repos/example/mod/pulls/7", {**w.pr, "draft": True}),
                   "attempt": lambda w: w.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "expired": lambda w: w.set_artifact(100, expired=True)}
        for name, change in changes.items():
            world = build_world()
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world)), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_protected_push_and_dispatch_run_from_the_commit_they_test(self):
        for event in ("push", "workflow_dispatch"):
            world = build_world(push=True, event=event)
            world.bundle["producer"]["event"] = event
            data, world.envelope = sealed_build_archive(world.plan, world.bundle["producer"])
            world.bundle["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
            world.publish(world.bundle, data)
            identity = world.plan["identity"]
            self.assertEqual(world.runs[42]["head_sha"], identity["tested_sha"])
            self.assertEqual(identity["tested_sha"], identity["controller_sha"])
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(self.call(world, Path(directory) / "output"), world.envelope)

    def test_historical_non_pr_subject_has_no_producer_run(self):
        world = World()
        protected_subject(world.plan, world.api)
        self.assertNotEqual(world.plan["identity"]["tested_sha"], world.plan["identity"]["controller_sha"])
        descriptor = ci_run_descriptor(world.plan, "build", "full", "build")
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as reads, \
                self.assertRaisesRegex(MbError, "runs from the commit it tests"):
            transport.download_completed_build(world.api, descriptor=descriptor, plan=world.plan,
                                               output=Path(directory) / "output")
        reads.assert_not_called()

    def test_existing_destination_other_plan_kind_and_rebuilt_bundle_reject_before_any_read(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as reads:
            with self.assertRaises(MbError):
                self.call(world, Path(directory))
            with self.assertRaises(MbError):
                self.call(world, "output")
            changed = copy.deepcopy(world.bundle)
            changed["plan_sha256"] = "f" * 64
            with self.assertRaises(MbError):
                self.call(world, Path(directory) / "output", descriptor=changed)
            target = ci_run_descriptor(world.plan, "build", "full", "target", unit_id="target-a")
            with self.assertRaises(MbError):
                self.call(world, Path(directory) / "output", descriptor=target)
            push = World(push=True)
            rebuilt = ci_run_descriptor(push.plan, "packaged", "rebuilt", "build")
            with self.assertRaisesRegex(MbError, "read by that run only"):
                transport.download_completed_build(world.api, descriptor=rebuilt, plan=push.plan,
                                                   output=Path(directory) / "output")
        reads.assert_not_called()

    def test_caller_documents_are_copied_on_entry(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory:
            with after_download(world.api, lambda: (world.plan.clear(), world.bundle.clear())):
                observed = self.call(world, Path(directory) / "output")
        self.assertEqual(observed, world.envelope)


class TargetSetTransportTests(unittest.TestCase):
    def call(self, world, output, **changes):
        arguments = {"descriptors": world.descriptors, "plan": world.plan, "run_id": 42, "run_attempt": 2,
                     "output": output, **changes}
        return transport.download_target_set(world.api, **arguments)

    def test_running_attempt_publishes_every_ordered_target_once(self):
        world = target_set_world()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "inputs"
            self.assertEqual(self.call(world, output), world.partitions)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["target-0", "target-1"])
            for index, partition in enumerate(world.partitions):
                self.assertEqual(verify_build_export(output / f"target-{index}", plan=world.plan),
                                 partition["envelope"])
            self.assertEqual(list(Path(directory).iterdir()), [output])
        # Source 4+3, run 2, jobs 2, the run's artifact listing 2 and two downloads of two requests.
        self.assertEqual(world.api.request_count, 17)
        self.assertEqual(world.api.mutations, [])

    def test_request_count_does_not_grow_with_metadata_reads_per_target(self):
        world = target_set_world()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "get_json", wraps=world.api.get_json) as reads:
            self.call(world, Path(directory) / "inputs")
        paths = [call.args[0] for call in reads.call_args_list]
        self.assertEqual(sum(path.endswith("/actions/runs/42/artifacts") for path in paths), 2)
        self.assertEqual(sum("/actions/artifacts/" in path for path in paths), 0)

    def test_missing_extra_reordered_duplicate_mixed_and_wrong_plan_sets_reject_before_any_read(self):
        mutations = [lambda d: d.pop(), lambda d: d.append(copy.deepcopy(d[0])), lambda d: d.reverse(),
                     lambda d: d[1]["producer"].update(graph_sha256="f" * 64),
                     lambda d: d[1].update(plan_sha256="f" * 64)]
        for index, mutate in enumerate(mutations):
            world = target_set_world()
            mutate(world.descriptors)
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            reads.assert_not_called()
        for changes in ({"run_id": 43}, {"run_attempt": 1}, {"run_id": True}):
            world = target_set_world()
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output", **changes)
            reads.assert_not_called()

    def test_duplicate_artifact_and_compressed_budget_reject_before_fetch(self):
        world = target_set_world()
        world.descriptors[1]["artifact"]["id"] = world.descriptors[0]["artifact"]["id"]
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as download, \
                self.assertRaises(MbError):
            self.call(world, Path(directory) / "output")
        download.assert_not_called()
        world = target_set_world()
        total = sum(descriptor["artifact"]["size"] for descriptor in world.descriptors)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(limits, "MAX_CI_TARGET_DOWNLOAD_BYTES", total - 1), \
                patch.object(world.api, "get_json") as reads, self.assertRaisesRegex(MbError, "compressed-byte"):
            self.call(world, Path(directory) / "output")
        reads.assert_not_called()

    def test_plan_policy_and_every_target_must_have_finished_and_sealed(self):
        def unsealed(job):
            job["steps"].pop(3)

        mutations = {"Shared Build / Plan protected Build": lambda j: j.update(conclusion="failure"),
                     "Shared Build / Verify protected policy": lambda j: j.update(conclusion="skipped"),
                     "Shared Build / Compile target target-a": lambda j: j.update(status="in_progress", conclusion=None),
                     "Shared Build / Compile target target-b": unsealed}
        for name, mutate in mutations.items():
            world = target_set_world()
            mutate(world.job(42, name))
            world.set_jobs(42, world.jobs[42])
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download") as download, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            download.assert_not_called()
        for extra in ({"name": "foreign job"}, {}):
            world = target_set_world()
            world.set_jobs(42, world.jobs[42] + [{**copy.deepcopy(world.jobs[42][2]), "id": 5, **extra}])
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")

    def test_queued_cancelled_failed_or_incoherent_producer_status_rejects(self):
        for status, conclusion in (("queued", None), ("completed", "cancelled"),
                                   ("completed", "failure"), ("in_progress", "success")):
            world = target_set_world()
            world.set_run(42, status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")

    def test_late_archive_failure_and_movement_publish_nothing(self):
        world = target_set_world()
        world.api.add_artifact(world.records[111], b"not a ZIP of the right digest")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MbError):
                self.call(world, Path(directory) / "output")
            self.assertEqual(list(Path(directory).iterdir()), [])
        changes = {"source": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40),
                   "attempt": lambda w: w.set_run(42, run_attempt=3),
                   "cancelled": lambda w: w.set_run(42, status="completed", conclusion="cancelled"),
                   "expired": lambda w: w.set_artifact(110, expired=True)}
        for name, change in changes.items():
            world = target_set_world()
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world), count=2), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_logical_export_budget_is_not_multiplied_by_targets(self):
        world = target_set_world()
        total = sum(file["size"] for partition in world.partitions for file in partition["envelope"]["files"])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(limits, "MAX_CI_EXPORT_TREE_BYTES", total - 1), self.assertRaises(MbError):
            self.call(world, Path(directory) / "output")

    def test_reads_of_one_command_share_immutable_objects(self):
        world = target_set_world()
        reads = CommandReads.of(world.api)
        with tempfile.TemporaryDirectory() as directory:
            transport.download_target_set(reads, descriptors=world.descriptors, plan=world.plan, run_id=42,
                                          run_attempt=2, output=Path(directory) / "first")
            before = world.api.request_count
            transport.download_target_set(reads, descriptors=world.descriptors, plan=world.plan, run_id=42,
                                          run_attempt=2, output=Path(directory) / "second")
        self.assertEqual(world.api.request_count - before, 16)  # the tested commit is not read again


if __name__ == "__main__":
    unittest.main()
