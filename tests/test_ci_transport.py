"""Real fake-API transport provenance; filesystem seams are explicit on Windows."""

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.build_ci.graph import BuildGraphV1, authenticate_graph
from mod_base.github.jobs import job_graph_sha256
from mod_base.errors import MbError
from mod_base.io import bounded_zip
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_BUILD_CALL, CI_BUILD_JOBS
from tests.helpers import ci_descriptor, ci_envelope
from tests.test_ci_protocol import protected_subject, seeded_graph, seeded_pr
from tests.test_ci_exports import partitions_fixture


def transport_fixture(*, target=False, event="pull_request_target", historical=False):
    plan, api, _ = seeded_pr()
    if event != "pull_request_target":
        protected_subject(plan, api, current=not historical)
    _, _, jobs = seeded_graph()
    api.add_jobs(42, 2, jobs)
    descriptor = ci_descriptor("target", unit_id="target-a") if target else ci_descriptor()
    for key in ("identity", "plan_sha256", "profile"):
        descriptor[key] = copy.deepcopy(plan[key])
    api_head = plan["identity"]["tested_sha"] if event == "push" else plan["identity"]["controller_sha"]
    descriptor["producer"].update(event=event, api_head_sha=api_head)
    descriptor["producer"]["graph_sha256"] = authenticate_graph(api, plan=plan, producer="build",
                                                              run_id=42, run_attempt=2)
    envelope = ci_envelope()
    for key in ("identity", "plan_sha256", "profile"):
        envelope[key] = copy.deepcopy(plan[key])
    if target:
        envelope.update(scope="target", target_id="target-a")
    envelope["producer"] = {key: copy.deepcopy(value) for key, value in descriptor["producer"].items()
                            if key != "upload_window"}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for file in envelope["files"]:
            data = (file["path"] + "\n").encode()
            file.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            archive.writestr(file["path"], data)
        archive.writestr("ci-envelope.json", canonical_json(envelope))
    data = stream.getvalue()
    descriptor["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
    producer, artifact = descriptor["producer"], descriptor["artifact"]
    run = {"id": 42, "run_attempt": 2, "workflow_id": 11, "path": producer["workflow_path"],
           "created_at": "2026-10-07T10:00:00Z", "event": event, "head_sha": api_head,
           "head_branch": "master", "head_repository": {"full_name": api.repository},
           "status": "completed", "conclusion": "success", "referenced_workflows": [
               {"path": f"{plan['identity']['kit']['repository']}/.github/workflows/build.yml@{plan['identity']['kit']['sha']}",
                "sha": plan["identity"]["kit"]["sha"]}]}
    api.add_run(run)
    if target:
        run.update(status="in_progress", conclusion=None)
        api.add_run(run)
        api.add_jobs(42, 2, [job for job in jobs if "target-a" in job["name"]])
    record = {"id": artifact["id"], "name": artifact["name"], "size_in_bytes": artifact["size"],
              "digest": artifact["digest"], "created_at": artifact["created_at"],
              "expires_at": artifact["expires_at"], "expired": False,
              "workflow_run": {"id": 42, "head_branch": "master", "head_sha": api_head}}
    api.add_artifact(record, data)
    return plan, api, descriptor, envelope, run, record, data


def target_set_fixture():
    plan, partitions = partitions_fixture()
    _, api, _, _, run, record, _ = transport_fixture(target=True)
    graph = BuildGraphV1()
    _, _, template_jobs = seeded_graph()
    steps = next(job["steps"] for job in template_jobs if job["steps"])
    jobs = [{**entry, "status": "completed", "steps": copy.deepcopy(steps) if entry["name"] in
             graph.sealed_jobs(plan) else []} for entry in graph.jobs(plan)]
    api.add_jobs(42, 2, [job for job in jobs if job["name"] not in
                        {f"{CI_BUILD_CALL} / {CI_BUILD_JOBS[key]}" for key in ("assemble", "gate")}])
    descriptors = []
    for partition in partitions:
        descriptor, envelope = partition["descriptor"], partition["envelope"]
        descriptor["producer"]["graph_sha256"] = job_graph_sha256(graph.jobs(plan))
        envelope["producer"] = {key: copy.deepcopy(value) for key, value in descriptor["producer"].items()
                                if key != "upload_window"}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for file in envelope["files"]:
                data = (file["path"] + "\n").encode()
                file.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                archive.writestr(file["path"], data)
            archive.writestr("ci-envelope.json", canonical_json(envelope))
        data = stream.getvalue()
        descriptor["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
        selected = descriptor["artifact"]
        api.add_artifact({**record, "id": selected["id"], "name": selected["name"],
                          "size_in_bytes": selected["size"], "digest": selected["digest"]}, data)
        descriptors.append(descriptor)
    return plan, api, descriptors, partitions, jobs, run


class TargetSetTransportTests(unittest.TestCase):
    def call(self, fixture, output):
        return transport.download_target_set(fixture[1], descriptors=fixture[2], plan=fixture[0],
            workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2, output=output)

    def test_complete_ordered_inputs_use_one_atomic_publication_and_shared_api_context(self):
        fixture = target_set_fixture()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, "atomic_directory", side_effect=lambda p, writer: writer(Path(directory), 0)) as publication, \
                patch.object(transport, "extract_build"), patch.object(transport, "validate_tree_entries"), \
                patch.object(transport, "verify_build_export", side_effect=[item["envelope"] for item in fixture[3]]):
            before = fixture[1].request_count
            self.assertEqual(self.call(fixture, Path(directory) / "output"), fixture[3])
            self.assertLess(fixture[1].request_count - before, 30)
        publication.assert_called_once()

    def test_missing_extra_reordered_duplicate_mixed_and_wrong_plan_sets_reject_before_fetch(self):
        mutations = [lambda d: d.pop(), lambda d: d.append(copy.deepcopy(d[0])), lambda d: d.reverse(),
                     lambda d: d[1]["artifact"].update(id=d[0]["artifact"]["id"]),
                     lambda d: d[1]["producer"].update(run_id=43),
                     lambda d: d[1].update(plan_sha256="f" * 64)]
        for index, mutate in enumerate(mutations):
            fixture = target_set_fixture()
            mutate(fixture[2])
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            download.assert_not_called()

    def test_additional_compressed_set_budget_applies_before_any_fetch(self):
        fixture = target_set_fixture()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(limits, "MAX_CI_TARGET_DOWNLOAD_BYTES", sum(d["artifact"]["size"] for d in fixture[2]) - 1), \
                patch.object(fixture[1], "download") as download, self.assertRaisesRegex(MbError, "compressed-byte"):
            self.call(fixture, Path(directory) / "output")
        download.assert_not_called()

    def test_policy_and_plan_success_are_required_before_fetch(self):
        for name in ("policy", "plan"):
            fixture = target_set_fixture()
            job = next(j for j in fixture[4] if j["name"] == f"{CI_BUILD_CALL} / {CI_BUILD_JOBS[name]}")
            job["conclusion"] = "skipped"
            fixture[1].add_jobs(42, 2, fixture[4])
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")

    def test_late_target_failure_cannot_return_partial_input_set(self):
        fixture = target_set_fixture()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, "atomic_directory", side_effect=lambda p, writer: writer(Path(directory), 0)), \
                patch.object(transport, "extract_build", side_effect=[[], MbError("second ZIP rejected")]), \
                patch.object(transport, "verify_build_export", return_value=fixture[3][0]["envelope"]), \
                self.assertRaisesRegex(MbError, "second ZIP"):
            self.call(fixture, Path(directory) / "output")

    def test_changed_source_at_last_download_forbids_set_publication(self):
        fixture = target_set_fixture()
        original = fixture[1].download
        count = 0
        def downloading(path, **kwargs):
            nonlocal count
            data = original(path, **kwargs)
            count += 1
            if count == 2:
                fixture[1].set_branch("master", "f" * 40, "e" * 40)
            return data
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, "atomic_directory", side_effect=lambda p, writer: writer(Path(directory), 0)), \
                patch.object(fixture[1], "download", side_effect=downloading), \
                patch.object(transport, "extract_build"), patch.object(transport, "validate_tree_entries"), \
                patch.object(transport, "verify_build_export", side_effect=[item["envelope"] for item in fixture[3]]), \
                self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "output")

    def test_original_expanded_export_budget_is_not_multiplied_by_targets(self):
        fixture = target_set_fixture()
        total = sum(file["size"] for partition in fixture[3] for file in partition["envelope"]["files"])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, "atomic_directory", side_effect=lambda p, writer: writer(Path(directory), 0)), \
                patch.object(limits, "MAX_CI_EXPORT_TREE_BYTES", total - 1), \
                patch.object(transport, "extract_build"), \
                patch.object(transport, "verify_build_export", side_effect=[item["envelope"] for item in fixture[3]]), \
                self.assertRaisesRegex(MbError, "original logical export budget"):
            self.call(fixture, Path(directory) / "output")


class TargetPartitionTransportTests(unittest.TestCase):
    def test_latest_and_attempt_workflow_ids_must_agree(self):
        for target in (False, True):
            fixture = transport_fixture(target=target)
            attempt = copy.deepcopy(fixture[4])
            attempt["workflow_id"] = 12
            fixture[1].add_response(f"/repos/{fixture[1].repository}/actions/runs/42/attempts/2", attempt)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                if target:
                    self.call(fixture, Path(directory) / "output")
                else:
                    transport.download_completed_build(fixture[1], descriptor=fixture[2], plan=fixture[0],
                        workflow_path=".github/workflows/build-gate.yml", output=Path(directory) / "output")

    def call(self, fixture, output, **changes):
        plan, api, descriptor, *_ = fixture
        values = dict(workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2,
                      target_id="target-a", output=output)
        values.update(changes)
        return transport.download_target_partition(api, descriptor=descriptor, plan=plan, **values)

    def test_finished_target_is_admitted_while_aggregate_and_gate_are_not_visible(self):
        fixture = transport_fixture(target=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract_build"), \
                patch.object(transport, "verify_build_export", return_value=fixture[3]), \
                patch.object(transport, "materialize_build_export", return_value=fixture[3]):
            self.assertEqual(self.call(fixture, Path(directory) / "output"), fixture[3])
        self.assertEqual(fixture[1].mutations, [])

    def test_assembler_context_and_artifact_target_must_be_exact(self):
        for changes in ({"run_id": 43}, {"run_attempt": 1}, {"run_id": True}, {"target_id": "foreign"}):
            fixture = transport_fixture(target=True)
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output", **changes)
        fixture = transport_fixture(target=True)
        fixture[2]["artifact"]["name"] = grammar.ci_artifact_name("build", 42, 2)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "output")

    def test_partial_graph_cannot_hide_duplicates_unknown_jobs_or_failed_unsealed_target(self):
        mutations = [lambda j: j.append(copy.deepcopy(j[0])),
                     lambda j: j.append({**j[0], "name": "foreign job"}),
                     lambda j: j[0].update(status="in_progress"),
                     lambda j: j[0].update(conclusion="skipped"),
                     lambda j: j[0]["steps"].pop(0),
                     lambda j: j[0]["steps"][0].update(completed_at="2026-10-07T10:01:30Z"),
                     lambda j: j[0].update(run_attempt=1)]
        for index, mutate in enumerate(mutations):
            fixture = transport_fixture(target=True)
            _, _, jobs = seeded_graph()
            jobs = [job for job in jobs if "target-a" in job["name"]]
            mutate(jobs)
            fixture[1].add_jobs(42, 2, jobs)
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            download.assert_not_called()

    def test_queued_cancelled_failed_or_incoherent_producer_status_rejects(self):
        for status, conclusion in (("queued", None), ("completed", "cancelled"),
                                   ("completed", "failure"), ("in_progress", "success")):
            fixture = transport_fixture(target=True)
            fixture[4].update(status=status, conclusion=conclusion)
            fixture[1].add_run(fixture[4])
            with self.subTest(status=status, conclusion=conclusion), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")

    def test_new_attempt_after_download_forbids_publication(self):
        fixture = transport_fixture(target=True)
        def extracted(*args):
            fixture[4]["run_attempt"] = 3
            fixture[1].add_run(fixture[4])
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract_build", side_effect=extracted), \
                patch.object(transport, "verify_build_export", return_value=fixture[3]), \
                patch.object(transport, "materialize_build_export") as copying, self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "output")
        copying.assert_not_called()

    def test_complete_build_route_still_requires_completed_full_run(self):
        fixture = transport_fixture()
        fixture[4].update(status="in_progress", conclusion=None)
        fixture[1].add_run(fixture[4])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            transport.download_completed_build(fixture[1], descriptor=fixture[2], plan=fixture[0],
                                                workflow_path=".github/workflows/build-gate.yml",
                                                output=Path(directory) / "output")


class CompletedBuildTransportTests(unittest.TestCase):
    def test_real_api_window_observed_after_serialization_does_not_rewrite_zip(self):
        fixture = transport_fixture()
        plan, api, descriptor, envelope, _, record, archive = fixture
        descriptor["producer"]["upload_window"] = {
            "started_at": "2026-10-07T10:03:00Z", "completed_at": "2026-10-07T10:04:00Z"}
        descriptor["artifact"]["created_at"] = record["created_at"] = "2026-10-07T10:03:30Z"
        _, _, jobs = seeded_graph()
        aggregate = next(job for job in jobs if job["name"] == f"{CI_BUILD_CALL} / {CI_BUILD_JOBS['assemble']}")
        aggregate["steps"][1].update(started_at="2026-10-07T10:03:00Z", completed_at="2026-10-07T10:04:00Z")
        api.add_jobs(42, 2, jobs)
        api.add_artifact(record, archive)
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract_build"), \
                patch.object(transport, "verify_build_export", return_value=envelope), \
                patch.object(transport, "materialize_build_export", return_value=envelope):
            self.assertEqual(self.call(fixture, Path(directory) / "output"), envelope)
        self.assertEqual(descriptor["artifact"]["digest"], "sha256:" + hashlib.sha256(archive).hexdigest())
        self.assertNotIn("upload_window", envelope["producer"])

    def test_historical_push_cannot_claim_a_different_live_controller(self):
        fixture = transport_fixture(event="push", historical=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract_build"), \
                patch.object(transport, "verify_build_export", return_value=fixture[3]), \
                patch.object(transport, "materialize_build_export", return_value=fixture[3]), self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "output")

    def test_current_push_and_historical_protected_dispatch_preserve_distinct_subjects(self):
        for event, historical in (("push", False), ("workflow_dispatch", True)):
            fixture = transport_fixture(event=event, historical=historical)
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory, \
                    patch.object(transport, "extract_build"), \
                    patch.object(transport, "verify_build_export", return_value=fixture[3]), \
                    patch.object(transport, "materialize_build_export", return_value=fixture[3]):
                self.assertEqual(self.call(fixture, Path(directory) / "output"), fixture[3])

    def test_real_zip_preflight_rejects_traversal_and_entry_overflow_before_publication(self):
        for hostile in (True, False):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("../escape" if hostile else "one", b"x")
                if not hostile:
                    archive.writestr("two", b"y")
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(limits, "MAX_CI_EXPORT_ENTRIES", 1), \
                    patch.object(bounded_zip.atomic, "atomic_directory") as publication, \
                    self.assertRaisesRegex(MbError, "unsafe entry|entry count|entries"):
                bounded_zip.extract_build(stream.getvalue(), Path(directory) / "output")
            publication.assert_not_called()

    def test_build_extraction_has_fixed_separate_bounds_without_widening_pages(self):
        with patch.object(bounded_zip, "_extract", return_value=[]) as extraction:
            bounded_zip.extract_build(b"zip", Path("output"))
            bounds = extraction.call_args.args[2]
            self.assertEqual(bounds.max_entries, limits.MAX_CI_EXPORT_ENTRIES)
            self.assertEqual(bounds.max_total_bytes, limits.MAX_CI_EXPORT_TREE_BYTES + limits.MAX_CI_ENVELOPE_BYTES)
            self.assertEqual(bounds.max_entry_bytes, limits.MAX_CI_EXPORT_FILE_BYTES)
        with self.assertRaises(MbError):
            bounded_zip.archive_limit(bounds)

    def call(self, fixture, output):
        plan, api, descriptor, *_ = fixture
        return transport.download_completed_build(api, descriptor=descriptor, plan=plan,
                                                  workflow_path=".github/workflows/build-gate.yml", output=output)

    def test_exact_numeric_download_and_checked_copy_order(self):
        fixture = transport_fixture()
        envelope = fixture[3]
        calls = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, "extract_build", side_effect=lambda *a: calls.append("extract_build")), \
                patch.object(transport, "verify_build_export", return_value=envelope), \
                patch.object(transport, "materialize_build_export", side_effect=lambda *a, **k: calls.append("copy") or envelope):
            self.assertEqual(self.call(fixture, Path(directory) / "output"), envelope)
        self.assertEqual(calls, ["extract_build", "copy"])
        self.assertEqual(fixture[1].mutations, [])

    def test_wrong_run_attempt_event_head_kit_and_conclusion_reject_before_download(self):
        mutations = [lambda r: r.update(run_attempt=3), lambda r: r.update(event="pull_request"),
                     lambda r: r.update(head_sha="f" * 40), lambda r: r.update(head_branch="other"),
                     lambda r: r.update(conclusion="failure"), lambda r: r.update(referenced_workflows=[]),
                     lambda r: r.update(path=".github/workflows/other.yml")]
        for index, mutate in enumerate(mutations):
            fixture = transport_fixture()
            mutate(fixture[4])
            fixture[1].add_run(fixture[4])
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            download.assert_not_called()

    def test_metadata_owner_name_digest_size_time_expiry_and_head_reject(self):
        mutations = [lambda r: r.update(expired=True), lambda r: r.update(name="mb-ci-build--43--a2"),
                     lambda r: r.update(digest="sha256:" + "f" * 64), lambda r: r.update(size_in_bytes=1),
                     lambda r: r.update(created_at="2026-10-07T10:01:31Z"),
                     lambda r: r.update(expires_at="2026-10-09T00:00:00Z"),
                     lambda r: r["workflow_run"].update(id=43),
                     lambda r: r["workflow_run"].update(head_sha="f" * 40)]
        for index, mutate in enumerate(mutations):
            fixture = transport_fixture()
            mutate(fixture[5])
            fixture[1].add_artifact(fixture[5], fixture[6])
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            download.assert_not_called()

    def test_graph_and_authenticated_upload_window_are_mandatory(self):
        for change in ("graph", "window"):
            fixture = transport_fixture()
            if change == "graph":
                fixture[2]["producer"]["graph_sha256"] = "f" * 64
            else:
                fixture[2]["producer"]["upload_window"]["started_at"] = "2026-10-07T10:00:59Z"
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")

    def test_digest_and_length_are_checked_before_extraction(self):
        for data in (b"bad", b"x" * len(transport_fixture()[6])):
            fixture = transport_fixture()
            with tempfile.TemporaryDirectory() as directory, patch.object(fixture[1], "download", return_value=data), \
                    patch.object(transport, "extract_build") as extraction, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            extraction.assert_not_called()

    def test_moved_source_after_download_and_wrong_envelope_never_publish(self):
        for change in ("source", "producer", "scope"):
            fixture = transport_fixture()
            envelope = copy.deepcopy(fixture[3])
            def extracted(*args):
                if change == "source":
                    fixture[1].set_branch("master", "f" * 40, "e" * 40)
                elif change == "producer":
                    envelope["producer"]["run_id"] = 43
                else:
                    envelope.update(scope="target", target_id="target-a")
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    patch.object(transport, "extract_build", side_effect=extracted), \
                    patch.object(transport, "verify_build_export", return_value=envelope), \
                    patch.object(transport, "materialize_build_export") as copying, self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
            copying.assert_not_called()

    def test_existing_destination_and_unenrolled_workflow_reject(self):
        fixture = transport_fixture()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory))
            fixture[2]["producer"]["workflow_path"] = ".github/workflows/other.yml"
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory) / "output")
