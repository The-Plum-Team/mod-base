"""A tested record is bound to the exact graph of its run and to real API execution times.

Job listings are the literal ones of ``tests/fixtures/ci_graphs``; the receipt never supplies a
time or a job name that the API does not confirm.
"""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci.graph import authenticate_gate_timeline
from mod_base.errors import MbError
from tests.helpers import ci_graph_jobs, ci_run_gate
from tests.test_ci_protocol import GRAPHS
from tests.test_ci_transport import ASSEMBLE, GATE, World, record_archive, runtime_archive

PACKAGED_GATE = "Shared Packaged E2E / Verify complete packaged E2E"
LANE = "Shared Packaged E2E / Run packaged lane lane-a"
AGGREGATE = "Shared Packaged E2E / Seal complete packaged results"
#: Every ``(gate, mode)`` whose run seals a receipt, with the subject it exists for.
GATES = (("build", "full", False), ("packaged", "pull-request", False), ("build", "full", True),
         ("packaged", "selected", True), ("build", "rebuilt", True), ("packaged", "rebuilt", True))


def add_gate(world, gate, mode):
    """Seal ``gate`` in a world that already holds the run (and the Build bundle) it needs."""

    producer = "build" if mode == "full" else "packaged"
    document = ci_run_gate(world.plan, gate, mode)
    if gate == "build":
        document["artifacts"] = [copy.deepcopy(world.bundle)]
    else:
        lane = b"runtime evidence of lane-a\n"
        runtime = world.publish(world.describe(producer, mode, "runtime", lane, unit_id="lane-a"), lane)
        record = world.describe(producer, mode, "results", b"-")["producer"]
        data, world.runtime_envelope = runtime_archive(world.plan, record, world.bundle)
        world.results = world.publish(world.describe(producer, mode, "results", data), data)
        document["artifacts"] = [runtime, copy.deepcopy(world.results)]
        document["owning_build"] = copy.deepcopy(world.bundle)
    data = record_archive(document)
    world.documents[gate] = document
    world.seals[gate] = world.publish(world.describe(producer, mode, f"tested-{gate}", data), data)
    return world


def gate_world(gate="build", mode=None, *, push=False):
    """The finished generation of the fixture subject up to the tested record of ``gate``.

    ``full`` is a Build run; ``pull-request`` and ``selected`` are packaged runs that consume the
    Build of a separate Build run; a ``rebuilt`` packaged run holds its own Build and both gates."""

    mode = mode or ("full" if gate == "build" else "pull-request")
    world = World(push=push or mode in ("selected", "rebuilt"))
    world.documents, world.seals = {}, {}
    if mode == "rebuilt":
        world.add_run("packaged", "packaged-rebuilt")
        world.add_bundle("packaged", "rebuilt")
    else:
        world.add_run("build", "build-full")
        world.add_bundle()
        if mode != "full":
            world.add_run("packaged", f"packaged-{mode}")
    return add_gate(world, gate, mode)


def pair_world():
    """A pull request with both of its gates sealed: the Build run and the packaged run."""

    world = gate_world("build")
    world.add_run("packaged", "packaged-pull-request")
    return add_gate(world, "packaged", "pull-request")


class GateTimelineTests(unittest.TestCase):
    def call(self, world, gate):
        return authenticate_gate_timeline(world.api, document=world.documents[gate],
                                          descriptor=world.seals[gate], plan=world.plan)

    def run_of(self, world, gate):
        return world.seals[gate]["producer"]["run_id"]

    def gate_job(self, world, gate):
        return world.job(self.run_of(world, gate), GATE if gate == "build" else PACKAGED_GATE)

    def reseed(self, world):
        for run_id, jobs in world.jobs.items():
            world.set_jobs(run_id, jobs)

    def test_every_sealing_mode_binds_its_literal_listing_read_only(self):
        for gate, mode, push in GATES:
            world = gate_world(gate, mode, push=push)
            producer = "build" if mode == "full" else "packaged"
            before = copy.deepcopy((world.documents[gate], world.seals[gate]))
            with self.subTest(gate=gate, mode=mode, push=push):
                self.assertEqual(self.call(world, gate), GRAPHS[producer, mode])
                self.assertEqual((world.documents[gate], world.seals[gate]), before)
                self.assertEqual(world.api.mutations, [])
                # One job listing and one attempt record (when the attempt started) per run
                # involved: a consumed Build of another run is the second.
                self.assertEqual(world.api.request_count, 4 if mode in ("pull-request", "selected") else 2)

    def test_a_rebuilt_build_gate_does_not_wait_for_the_packaged_half_of_its_run(self):
        world = gate_world("build", "rebuilt")
        self.assertGreater(world.job(43, PACKAGED_GATE)["completed_at"], self.gate_job(world, "build")["completed_at"])
        self.call(world, "build")
        world = gate_world("packaged", "rebuilt")
        world.job(43, GATE)["completed_at"] = "2026-10-07T10:11:01Z"
        self.reseed(world)
        with self.assertRaisesRegex(MbError, "prerequisites"):
            self.call(world, "packaged")

    def test_gate_cannot_validate_before_its_prerequisites_finish(self):
        for gate, name in (("build", "Verify pinned mod-base / Authenticate the pinned kit"),
                           ("build", "Shared Build / Plan protected Build"),
                           ("build", "Shared Build / Verify protected policy"),
                           ("build", "Shared Build / Compile target target-a"), ("build", ASSEMBLE),
                           ("packaged", "Shared Packaged E2E / Authenticate exact Build"), ("packaged", LANE),
                           ("packaged", AGGREGATE)):
            world = gate_world(gate)
            verifier = "2026-10-07T10:04:00Z" if gate == "build" else "2026-10-07T10:11:00Z"
            late = verifier[:-3] + "01Z"
            world.job(self.run_of(world, gate), name)["completed_at"] = late
            self.reseed(world)
            with self.subTest(gate=gate, name=name), self.assertRaises(MbError):
                self.call(world, gate)

    def test_equal_whole_second_boundary_is_allowed(self):
        world = gate_world("build")
        world.job(42, ASSEMBLE)["completed_at"] = "2026-10-07T10:04:00Z"
        world.job(42, ASSEMBLE)["steps"][-1].update(started_at="2026-10-07T10:04:00.000Z",
                                                    completed_at="2026-10-07T10:04:00.000Z")
        self.reseed(world)
        self.call(world, "build")
        # Times are compared as whole seconds: a fraction never orders two events of one second.
        world = gate_world("build")
        world.job(42, ASSEMBLE)["completed_at"] = "2026-10-07T10:04:00.900Z"
        self.gate_job(world, "build")["steps"][1]["started_at"] = "2026-10-07T10:04:00.100Z"
        self.reseed(world)
        self.call(world, "build")

    def test_skipped_caller_jobs_never_hold_a_gate_back(self):
        world = gate_world("packaged")
        for name in ("Packaged E2E deferred for draft", "Select exact Build", "Shared Build"):
            job = world.job(43, name)
            self.assertEqual(job["conclusion"], "skipped")
            job.update(started_at="2026-10-07T10:30:00Z", completed_at="2026-10-07T10:30:00Z")
        self.reseed(world)
        self.call(world, "packaged")

    def test_source_window_must_equal_the_actual_upload_of_its_job(self):
        for gate, source in (("build", "bundle"), ("packaged", "runtime"), ("packaged", "results"),
                             ("packaged", "owning")):
            for key in ("started_at", "completed_at"):
                world = gate_world(gate)
                document = world.documents[gate]
                descriptor = (document["owning_build"] if source == "owning"
                              else document["artifacts"][1 if source == "results" else 0])
                window = descriptor["producer"]["upload_window"]
                # One second wider on either side: still a valid descriptor, no longer the real upload.
                window[key] = "2026-10-07T10:00:59Z" if key == "started_at" else window[key][:-2] + "1Z"
                with self.subTest(gate=gate, source=source, key=key), self.assertRaisesRegex(MbError, "actual API"):
                    self.call(world, gate)

    def test_gate_window_must_equal_the_selected_record_upload(self):
        for gate in ("build", "packaged"):
            world = gate_world(gate)
            upload = self.gate_job(world, gate)["steps"][2]
            upload["started_at"] = upload["started_at"].replace(":00.000", ":01.000")
            self.reseed(world)
            with self.subTest(gate=gate), self.assertRaisesRegex(MbError, "tested-record"):
                self.call(world, gate)

    def test_missing_duplicate_failed_or_overlapping_gate_steps_reject(self):
        mutations = [lambda j: j["steps"].pop(2), lambda j: j["steps"].pop(1),
                     lambda j: j["steps"].append(copy.deepcopy(j["steps"][1])),
                     lambda j: j["steps"][1].update(conclusion="failure"),
                     lambda j: j["steps"][1].update(completed_at="2026-10-07T10:05:01.000Z"),
                     lambda j: j["steps"][1].update(started_at="2026-10-07T10:03:39.000Z"),
                     lambda j: j.pop("started_at"), lambda j: j.update(completed_at="2026-10-07T09:00:00Z"),
                     lambda j: j.update(completed_at="2026-10-07T10:05:59Z")]
        for index, mutate in enumerate(mutations):
            world = gate_world("build")
            job = self.gate_job(world, "build")
            self.assertEqual([step["name"] for step in job["steps"]][1:3],
                             ["Validate frozen native exports", "Upload sealed outputs"])
            mutate(job)
            self.reseed(world)
            with self.subTest(index=index), self.assertRaises(MbError):
                self.call(world, "build")

    def test_owning_build_graph_and_completion_are_independent(self):
        for mutation in ("missing", "late", "digest", "seal", "deferred", "extra"):
            world = gate_world("packaged")
            jobs = world.jobs[42]
            if mutation == "missing":
                jobs.pop()
            elif mutation == "late":
                world.job(42, GATE)["completed_at"] = "2026-10-07T10:11:01Z"
            elif mutation == "digest":
                world.documents["packaged"]["owning_build"]["producer"]["graph_sha256"] = "f" * 64
            elif mutation == "seal":
                world.job(42, ASSEMBLE)["steps"].pop(2)
            elif mutation == "deferred":
                jobs[:] = ci_graph_jobs("build-deferred")
            else:
                jobs[:] = ci_graph_jobs("build-full-extra-job")
            self.reseed(world)
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(world, "packaged")

    def test_gate_graph_rejects_other_listings_modes_and_digests(self):
        for listing in ("build-deferred", "build-reuse", "build-attest-only", "build-full-extra-job",
                        "build-full-missing-job", "build-full-duplicated-job", "build-full-wrong-conclusion"):
            world = gate_world("build")
            world.set_jobs(42, ci_graph_jobs(listing))
            with self.subTest(listing=listing), self.assertRaises(MbError):
                self.call(world, "build")
        world = gate_world("build")
        for record in (world.documents["build"], world.seals["build"], world.documents["build"]["artifacts"][0]):
            record["producer"]["graph_sha256"] = "f" * 64
        with self.assertRaisesRegex(MbError, "wrong gate graph"):
            self.call(world, "build")
        # A standalone packaged record that claims the other standalone mode of its run.
        world = gate_world("packaged", "selected")
        world.set_jobs(43, ci_graph_jobs("packaged-rebuilt"))
        with self.assertRaisesRegex(MbError, "exact job graph mismatch"):
            self.call(world, "packaged")

    def test_record_and_descriptor_must_be_one_binding(self):
        world = gate_world("build")
        other = gate_world("packaged")
        with self.assertRaises(MbError):
            authenticate_gate_timeline(world.api, document=world.documents["build"],
                                       descriptor=other.seals["packaged"], plan=world.plan)
        world.seals["build"]["plan_sha256"] = "f" * 64
        with self.assertRaises(MbError):
            self.call(world, "build")


if __name__ == "__main__":
    unittest.main()
