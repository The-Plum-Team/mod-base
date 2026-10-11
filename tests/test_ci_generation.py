"""A consumer selects the tested generation of a default-branch commit, full or reused (KQ9-1).

Two worlds on a fake GitHub with real record archives and literal job listings:

* a full generation of a push (``tests.test_ci_transport.World``): the Build run and the packaged
  run that selected its bundle, or a packaged run that rebuilt and sealed both gates;
* a reused generation (``tests.ci_reuse``): pull request 7 merged as the pushed commit with both
  of its original gates, and the push's Build and packaged runs that each sealed a reuse reference
  to them.

``generation.select_generation`` is called directly, and ``ci select-generation`` through the real
``ci`` parser on a real state directory.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, generation
from mod_base.build_ci.graph import run_graph, sealed_upload, upload_job_name
from mod_base.build_ci.records import results_index, reuse_reference, validate_generation
from mod_base.build_ci.selection import producer_record
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from mod_base.workflow import find_job
from tests import ci_mod_harness as h
from tests.ci_attempt import (ATTEMPT, RUN, config_sha256, expand, lane_archive, record_zip, runtime_input_sha256,
                              sealed, validation)
from tests.ci_reuse import BUNDLE, LANE, ORIGINAL_RUN, PACKAGED_SEAL, REFERENCE, REUSE_LISTING, Merged, ReuseCase
from tests.helpers import ci_api_artifact, ci_api_run, ci_graph_jobs
from tests.test_ci_gate_timeline import add_gate, gate_world
from tests.test_ci_gate_transport import reseal
from tests.test_ci_transport import World, exported, sealed_build_archive

#: Requests of a full selected generation: the live subject, both newest runs with their run,
#: jobs and artifact listings, both tested records (sources, timeline, download) and everything
#: mutable once more. The bundle adds its download (2).
FULL_REQUESTS = 27
#: Requests of a reused generation: the subject, both reuse runs and both references as
#: ``reuse.download_reuse_reference`` reads one, the original pair once, and the rechecks.
REUSE_REQUESTS = 52


def full_world(mode: str = "selected"):
    """A push of the transport fixture with both gates sealed: by the Build run and the packaged
    run that selected its bundle, or both by a packaged run that rebuilt (no Build run)."""

    if mode == "rebuilt":
        return add_gate(gate_world("packaged", "rebuilt", push=True), "build", "rebuilt")
    world = gate_world("build", "full", push=True)
    world.add_run("packaged", "packaged-selected")
    return add_gate(world, "packaged", "selected")


class RealBundle(Merged):
    """Pull request 7 whose complete Build is a real sealed archive, so it can be copied."""

    def publish(self, producer, kind, unit_id, data, artifact_id, **changes):
        if kind == "build":
            data = sealed_build_archive(self.plan, self.producer(producer))[0]
        return super().publish(producer, kind, unit_id, data, artifact_id, **changes)


class RealEvidence(RealBundle):
    """Pull request 7 whose bundle, lane exports and results index are real sealed archives, and
    whose packaged gate carries the lanes' real report hashes."""

    def publish(self, producer, kind, unit_id, data, artifact_id, **changes):
        if kind == "build":
            data, self.build_envelope = sealed_build_archive(self.plan, self.producer(producer))
        elif kind == "runtime":
            archive, envelope = lane_archive(self.plan, self.producer(producer), self.bundle, unit_id)
            record, files = validation(self.plan, hook="verify_runtime", unit_id=unit_id, run_id=ORIGINAL_RUN[producer],
                                       input_sha256=runtime_input_sha256(
                                           self.plan, canonical_sha256(self.build_envelope), envelope))
            data = sealed(archive, record, files)
            self.lane_hashes = {**getattr(self, "lane_hashes", {}), unit_id: {
                "envelope_sha256": canonical_sha256(envelope), "validation_sha256": canonical_sha256(record),
                "report_sha256": record["reports"][0]["sha256"]}}
        elif kind == "results":
            index = results_index(plan=self.plan, producer=self.producer(producer), owning_build=self.bundle,
                                  build_envelope_sha256=canonical_sha256(self.build_envelope),
                                  lanes=[{"descriptor": descriptor, **self.lane_hashes[lane["id"]]}
                                         for lane, descriptor in zip(self.plan["lanes"], self.lanes)])
            data = record_zip(grammar.CI_RESULTS_NAME, canonical_json(index))
        return Merged.publish(self, producer, kind, unit_id, data, artifact_id, **changes)

    def receipts(self, units):
        if units != "lanes":
            return super().receipts(units)
        return [{"unit_id": lane["id"], "native_contract_sha256": lane["native_contract_sha256"],
                 "report_sha256": self.lane_hashes[lane["id"]]["report_sha256"]} for lane in self.plan["lanes"]]


def add_reuse_run(world: Merged, caller: str, *, source=None, listing: str | None = None,
                  artifact_id: int | None = None) -> dict:
    """The push's finished run of ``caller`` that sealed a reuse reference to ``source`` (the pull
    request's pair by default); returns the reference."""

    plan, api = world.covered, world.api
    run_id = RUN[caller]
    api.add_run(ci_api_run(plan, caller))
    jobs = expand(ci_graph_jobs(listing or REUSE_LISTING[caller]), plan)
    api.add_jobs(run_id, ATTEMPT, jobs)
    producer = producer_record(plan, caller=caller, run_id=run_id, run_attempt=ATTEMPT, event="push",
                               graph_sha256=run_graph(caller, "reuse").sha256(plan))
    document = reuse_reference(plan=plan, producer=producer, source=source or world.source)
    data = record_zip(grammar.CI_REUSE_NAME, canonical_json(document))
    created = sealed_upload(find_job(jobs, upload_job_name(caller, "reuse", None), run_attempt=ATTEMPT))[0]
    expires = (grammar.parse_timestamp(created) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    artifact_id = artifact_id or REFERENCE + (0 if caller == "build" else 1)
    record = ci_api_artifact({
        "identity": plan["identity"], "producer": {"run_id": run_id, "api_head_sha": plan["identity"]["tested_sha"]},
        "artifact": {"id": artifact_id, "name": grammar.ci_artifact_name("reuse", run_id, ATTEMPT),
                     "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                     "created_at": created, "expires_at": expires}})
    api.add_artifact(record, data)
    world.reference_records = {**getattr(world, "reference_records", {}), artifact_id: (record, data)}
    return document


class GenerationCase(ReuseCase):
    def select(self, world, *, plan=None, with_build=False, **changes):
        """Select into a new output; return ``(document, output)``. Nothing is ever written to
        GitHub, and the scratch directory holds nothing but a published output."""

        self.outputs = getattr(self, "outputs", 0) + 1
        output = self.scratch / f"generation-{self.outputs}"
        arguments = {"plan": plan or world.covered, "output": output, "with_build": with_build, **changes}
        try:
            return generation.select_generation(world.api, **arguments), output
        finally:
            self.assertEqual([path.name for path in self.scratch.iterdir() if not path.name.startswith("generation-")],
                             [], "a temporary download was left behind")
            self.assertEqual(world.api.mutations, [])

    def absent(self, world, reason: str, **changes) -> MbError:
        with self.assertRaises(generation.GenerationUnavailable) as caught:
            self.select(world, **changes)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertFalse((self.scratch / f"generation-{self.outputs}").exists())
        return caught.exception

    def rejected(self, world, message: str = "", **changes) -> MbError:
        with self.assertRaisesRegex(MbError, message) as caught:
            self.select(world, **changes)
        self.assertNotIsInstance(caught.exception, Unavailable)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertFalse((self.scratch / f"generation-{self.outputs}").exists())
        return caught.exception

    def reused(self, *, lanes: int = 1, real_bundle: bool = False, real: bool = False, producer: str | None = None,
               **merged) -> Merged:
        """The reused generation: pull request 7 and the push's two reuse runs. ``real_bundle``
        makes the Build a real sealed archive, ``real`` the lanes and the results index too;
        ``producer`` is the producer of the state's identity record (a Build job's by default)."""

        attempt = self.attempt(listing=REUSE_LISTING["build"], caller="build", push=True, lanes=lanes,
                               producer=producer)
        world = (RealEvidence if real else RealBundle if real_bundle else Merged)(attempt, **merged)
        for caller in ("build", "packaged"):
            add_reuse_run(world, caller)
        return world


class FullGenerationTests(GenerationCase):
    def full(self, mode: str = "selected"):
        world = full_world(mode)
        world.covered = world.plan
        return world

    def test_a_selected_full_generation_is_published_with_its_bundle(self) -> None:
        world = self.full()
        document, output = self.select(world, with_build=True)
        self.assertEqual(world.api.request_count, FULL_REQUESTS + 2)
        self.assertLess(FULL_REQUESTS + 2, limits.MAX_CI_SELECT_GENERATION_REQUESTS)
        raw = (output / generation.GENERATION_NAME).read_bytes()
        self.assertEqual(raw, canonical_json(document))
        self.assertEqual(load_document(raw, kind="mod-base.ci.generation"), document)
        self.assertIs(validate_generation(document, plan=world.plan), document)
        self.assertEqual((document["mode"], document["references"]), ("full", None))
        self.assertEqual(document["source"], {"identity": world.plan["identity"], "plan_sha256": world.plan["plan_sha256"],
                                              "profile": world.plan["profile"], "build_seal": world.seals["build"],
                                              "packaged_seal": world.seals["packaged"]})
        packaged = world.documents["packaged"]
        self.assertEqual(document["build"], world.bundle)
        self.assertEqual(document["build"], packaged["owning_build"])
        self.assertEqual(document["results"], packaged["artifacts"][-1])
        self.assertEqual([lane["descriptor"] for lane in document["lanes"]], packaged["artifacts"][:-1])
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["build", "generation.json"])
        self.assertIn(grammar.CI_ENVELOPE_NAME, exported(output / "build"))
        self.assertEqual(json.loads((output / "build" / grammar.CI_ENVELOPE_NAME).read_bytes()), world.envelope)

    def test_without_the_bundle_only_the_record_is_published(self) -> None:
        world = self.full()
        with mock.patch.object(world.api, "download", wraps=world.api.download) as downloads:
            document, output = self.select(world)
        self.assertEqual([path.name for path in output.iterdir()], ["generation.json"])
        self.assertEqual(world.api.request_count, FULL_REQUESTS)
        # Only the two tested records were read; no bundle and no lane.
        self.assertEqual(sorted(call.args[0].split("/")[-2] for call in downloads.call_args_list), ["200", "201"])
        self.assertEqual(document["mode"], "full")

    def test_a_packaged_run_that_rebuilt_is_a_generation_of_its_own(self) -> None:
        world = self.full("rebuilt")
        document, output = self.select(world, with_build=True)
        seals = document["source"]
        self.assertEqual({seals["build_seal"]["producer"]["run_id"], seals["packaged_seal"]["producer"]["run_id"]},
                         {43})
        self.assertEqual(document["build"], world.bundle)
        self.assertIn(grammar.CI_ENVELOPE_NAME, exported(output / "build"))

    def test_no_run_a_run_in_progress_and_a_failed_run_select_nothing(self) -> None:
        world = self.full()
        for run_id in (42, 43):
            world.set_run(run_id, status="in_progress", conclusion=None)
            with self.subTest(pending=run_id):
                self.absent(world, "ci-generation-pending")
            world.set_run(run_id, status="completed", conclusion="failure")
            with self.subTest(failed=run_id):
                self.absent(world, "ci-generation-failed")
            world.set_run(run_id, conclusion="success")
        self.select(world)

        world = self.full()
        world.set_run(43, status="queued", conclusion=None)
        world.set_run(42, conclusion="cancelled")
        self.absent(world, "ci-generation-pending")

        nothing = World(push=True)
        nothing.covered = nothing.plan
        self.absent(nothing, "ci-generation-absent")
        # A Build run alone is no generation either: the packaged run decides.
        nothing.add_run("build", "build-full")
        self.absent(nothing, "ci-generation-absent")

    def test_the_two_callers_must_form_one_generation(self) -> None:
        # A Build run that reused beside a packaged run that tested in full.
        world = self.full()
        world.set_jobs(42, ci_graph_jobs("build-reuse"))
        self.rejected(world, "is a selected run, but the Build caller's newest run for the commit is reuse")
        # A packaged run that rebuilt beside a full Build run of the same commit.
        world = self.full("rebuilt")
        world.add_run("build", "build-full")
        self.rejected(world, "is a rebuilt run, but the Build caller's newest run for the commit is full")
        # A packaged run that selected a bundle while the Build caller has no run.
        world = self.full()
        world.set_run(42, head_sha="3" * 40)
        world.api.add_run(world.runs[42])
        self.rejected(world, "newest run for the commit is absent")

    def test_evidence_that_is_gone_is_unavailable_not_corrupt(self) -> None:
        cases = {"bundle expired": lambda world: world.set_artifact(100, expired=True),
                 "lane expired": lambda world: world.set_artifact(101, expired=True),
                 "record expired": lambda world: world.set_artifact(201, expired=True)}
        for name, change in cases.items():
            world = self.full()
            change(world)
            with self.subTest(case=name):
                self.absent(world, "ci-generation-evidence-unavailable")
        world = self.full()
        gone = ApiError("Not Found", status=404, method="GET", path="/repos/example/mod/actions/artifacts/200/zip")
        with mock.patch.object(world.api, "download", side_effect=gone):
            self.absent(world, "ci-generation-evidence-unavailable")
        failure = ApiError("Server Error", status=502, method="GET", path="/repos/example/mod/actions/artifacts/200/zip")
        world = self.full()
        with mock.patch.object(world.api, "download", side_effect=failure), self.assertRaises(ApiError):
            self.select(world)

    def test_what_is_wrong_is_rejected(self) -> None:
        world = self.full()
        reseal(world, "packaged", raw=canonical_json(world.documents["packaged"]) + b"\n")
        self.rejected(world, "canonical JSON")

        world = self.full()
        world.set_run(42, run_attempt=3)
        self.rejected(world)

        world = self.full()
        world.api.set_branch("master", "f" * 40, "e" * 40)
        self.rejected(world, "protected controller has moved")

        world = self.full()
        pull_request = copy.deepcopy(world.plan)
        pull_request["identity"]["pr_number"] = 7
        with self.assertRaises(MbError):
            generation.select_generation(world.api, plan=pull_request, output=self.scratch / "out")
        self.assertFalse((self.scratch / "out").exists())

        world = self.full()
        (self.scratch / "generation").mkdir()
        with self.assertRaisesRegex(MbError, "preexisting output"):
            generation.select_generation(world.api, plan=world.plan, output=self.scratch / "generation")


class ReusedGenerationTests(GenerationCase):
    def test_a_reused_generation_names_the_merged_pull_request_and_its_original_evidence(self) -> None:
        world = self.reused(real_bundle=True)
        with mock.patch.object(world.api, "download", wraps=world.api.download) as downloads:
            document, output = self.select(world, with_build=True)
        self.assertEqual(world.api.request_count, REUSE_REQUESTS + 2)
        self.assertLess(REUSE_REQUESTS + 2, limits.MAX_CI_SELECT_GENERATION_REQUESTS)
        self.assertIs(validate_generation(document, plan=world.covered), document)
        self.assertEqual(load_document(canonical_json(document), kind="mod-base.ci.generation"), document)
        self.assertEqual(document["mode"], "reuse")
        self.assertEqual(document["source"], world.source)
        # The covered commit and the tested pull request stay apart; nothing is rewritten.
        self.assertEqual((document["identity"]["pr_number"], document["source"]["identity"]["pr_number"]), (0, 7))
        self.assertEqual(document["build"], world.bundle)
        self.assertEqual(document["results"], world.results)
        self.assertEqual([lane["descriptor"] for lane in document["lanes"]], world.lanes)
        self.assertEqual({key: reference["producer"]["run_id"] for key, reference in document["references"].items()},
                         {"build": RUN["build"], "packaged": RUN["packaged"]})
        self.assertEqual([int(call.args[0].split("/")[-2]) for call in downloads.call_args_list],
                         [REFERENCE, REFERENCE + 1, 600, 601, BUNDLE])
        envelope = json.loads((output / "build" / grammar.CI_ENVELOPE_NAME).read_bytes())
        self.assertEqual(envelope["identity"], world.plan["identity"])

    def test_several_lanes_keep_plan_order(self) -> None:
        world = self.reused(lanes=3)
        document, _ = self.select(world)
        self.assertEqual([lane["id"] for lane in document["lanes"]], [lane["id"] for lane in world.covered["lanes"]])
        self.assertEqual(world.api.request_count, REUSE_REQUESTS)

    def test_chosen_lanes_are_kept_as_verified_exports(self) -> None:
        world = self.reused(lanes=3, real=True)
        chosen = (world.covered["lanes"][2]["id"], world.covered["lanes"][0]["id"])
        document, output = self.select(world, lanes=chosen, source_config_sha256=config_sha256())
        # The index (2) and each chosen lane (2): the other lane is never downloaded.
        self.assertEqual(world.api.request_count, REUSE_REQUESTS + 2 + 2 * len(chosen))
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["generation.json", "lanes"])
        self.assertEqual(sorted(path.name for path in (output / "lanes").iterdir()), sorted(chosen))
        for lane_id in chosen:
            root = output / "lanes" / lane_id
            self.assertEqual(exported(root), sorted([grammar.CI_RUNTIME_ENVELOPE_NAME, f"lanes/{lane_id}/result.json",
                                                     f"lanes/{lane_id}/runtime.log"]))
            envelope = json.loads((root / grammar.CI_RUNTIME_ENVELOPE_NAME).read_bytes())
            # A reused lane keeps the identity it ran under and names the original bundle.
            self.assertEqual((envelope["identity"], envelope["owning_build"]), (world.plan["identity"], world.bundle))
        self.assertEqual(document["mode"], "reuse")

    def test_lanes_need_the_config_and_planned_ids(self) -> None:
        world = self.reused(real=True)
        lane = world.covered["lanes"][0]["id"]
        for changes in ({"lanes": (lane,)}, {"lanes": ("lane-zz",), "source_config_sha256": config_sha256()},
                        {"lanes": (lane, lane), "source_config_sha256": config_sha256()}, {"lanes": [lane]}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.select(world, **changes)
        self.assertEqual(world.api.request_count, 0)

    def test_a_lane_is_checked_against_the_index_and_the_gate(self) -> None:
        # The packaged gate sealed another report for the lane than the results index holds.
        world = self.reused(real=True)
        world.documents["packaged"]["native_receipts"][0]["report_sha256"] = "0" * 64
        world.seal("packaged")
        for caller in ("build", "packaged"):
            add_reuse_run(world, caller)
        lane = world.covered["lanes"][0]["id"]
        self.rejected(world, "disagree on a lane", lanes=(lane,), source_config_sha256=config_sha256())
        # A lane verified under another protected config is refused.
        world = self.reused(real=True)
        self.rejected(world, lanes=(lane,), source_config_sha256="0" * 64)
        # A lane that expires is gone, not corrupt; nothing is published.
        world = self.reused(real=True)
        world.set_artifact(LANE, expired=True)
        self.absent(world, "ci-generation-evidence-unavailable", lanes=(lane,), source_config_sha256=config_sha256())

    def test_a_mixed_generation_is_rejected(self) -> None:
        # The Build caller tested in full while the packaged caller reused.
        attempt = self.attempt(listing="build-full", caller="build", push=True)
        world = Merged(attempt)
        attempt.complete("build-full")
        add_reuse_run(world, "packaged")
        self.rejected(world, "is a reuse run, but the Build caller's newest run for the commit is full")

        # Two references that name different original generations.
        world = self.reused()
        other = copy.deepcopy(world.source)
        other["packaged_seal"]["artifact"]["expires_at"] = "2026-10-13T12:00:00Z"
        add_reuse_run(world, "packaged", source=other)
        self.rejected(world, "name different original generations")

        # A reuse run that also lists evidence of its own attempt.
        world = self.reused()
        record, data = world.reference_records[REFERENCE]
        extra = {**copy.deepcopy(record), "id": 990, "name": grammar.ci_artifact_name("build", RUN["build"], ATTEMPT)}
        world.api.add_artifact(extra, data)
        self.rejected(world, "mixed generation")

    def test_original_evidence_that_is_gone_is_unavailable(self) -> None:
        for name, artifact_id in (("bundle", BUNDLE), ("lane", LANE), ("packaged record", PACKAGED_SEAL)):
            world = self.reused()
            world.set_artifact(artifact_id, expired=True)
            with self.subTest(gone=name):
                self.absent(world, "ci-generation-evidence-unavailable")
        world = self.reused()
        record, data = world.reference_records[REFERENCE + 1]
        world.api.add_artifact({**record, "expired": True}, data)
        self.absent(world, "ci-generation-evidence-unavailable")

    def test_corrupt_original_evidence_is_rejected(self) -> None:
        world = self.reused()
        world.seal("packaged", raw=canonical_json(world.documents["packaged"]) + b"\n")
        add_reuse_run(world, "build")
        add_reuse_run(world, "packaged")
        self.rejected(world, "canonical JSON")

    def test_a_reuse_run_in_progress_waits(self) -> None:
        world = self.reused()
        world.api.add_run(ci_api_run(world.covered, "packaged", status="in_progress", conclusion=None))
        self.absent(world, "ci-generation-pending")


class CommandTests(GenerationCase):
    def command(self, world, *flags):
        attempt = world.attempt
        output, github_output = self.scratch / "generation", self.scratch.parent / "github-output"

        def client(environ, *, writable=False, max_requests=None):
            self.assertFalse(writable)
            self.assertEqual(max_requests, limits.MAX_CI_SELECT_GENERATION_REQUESTS)
            return world.api

        argv = ["ci", "select-generation", "--repo", str(h.MOD), "--config", str(h.MOD / "site" / "mod-base.json"),
                "--state", str(attempt.state), "--output", str(output), "--github-output", str(github_output), *flags]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "environ", return_value=attempt.environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        outputs = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
        return code, stdout.getvalue(), stderr.getvalue(), outputs, output

    def test_the_command_publishes_a_reused_generation(self) -> None:
        world = self.reused(real_bundle=True)
        code, stdout, stderr, outputs, output = self.command(world, "--with-build")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(outputs, {"generation": "reuse", "reason": "selected"})
        self.assertEqual(stdout, "select-generation: reuse generation of pull request #7, Build run 142 attempt 2, "
                                 "packaged run 143 attempt 2\n")
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["build", "generation.json"])

    def test_the_command_keeps_every_lane(self) -> None:
        world = self.reused(lanes=2, real=True)
        code, stdout, stderr, outputs, output = self.command(world, "--all-lanes")
        self.assertEqual((code, stderr, outputs), (0, "", {"generation": "reuse", "reason": "selected"}))
        self.assertEqual(sorted(path.name for path in (output / "lanes").iterdir()),
                         sorted(lane["id"] for lane in world.covered["lanes"]))

    def test_a_consumer_job_reads_the_generation_of_its_subject(self) -> None:
        world = self.reused(producer="generation")
        self.assertEqual(world.attempt.record["producer"], "generation")
        code, stdout, stderr, outputs, output = self.command(world)
        self.assertEqual((code, stderr, outputs), (0, "", {"generation": "reuse", "reason": "selected"}))
        self.assertEqual([path.name for path in output.iterdir()], ["generation.json"])

    def test_the_command_reports_an_absent_generation_with_exit_3(self) -> None:
        world = self.reused()
        world.set_artifact(BUNDLE, expired=True)
        code, stdout, stderr, outputs, output = self.command(world)
        self.assertEqual((code, stdout), (3, ""))
        self.assertTrue(stderr.startswith("mod_base: ci-generation-evidence-unavailable: "), stderr)
        self.assertEqual(outputs, {"generation": "none", "reason": "ci-generation-evidence-unavailable"})
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
