"""Full gate-record numeric-ID/API admission; Windows filesystem seams are explicit."""

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.test_ci_gate_timeline import timeline_fixture
from tests.test_ci_protocol import seeded_pr


def gate_transport_fixture(gate="build", *, raw=None, filename=None):
    plan, _, document, descriptor, jobs = timeline_fixture(gate)
    _, api, _ = seeded_pr()
    for kind, entries in jobs.items():
        api.add_jobs(42 if kind == "build" else 43, 2, entries)
    raw = canonical_json(document) if raw is None else raw
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(grammar.CI_GATE_NAME if filename is None else filename, raw)
    data = stream.getvalue()
    descriptor["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
    sources = list(document["artifacts"])
    if document["owning_build"]:
        sources.append(document["owning_build"])
    records = {}
    runs = {}
    for source in [descriptor, *sources]:
        producer, selected = source["producer"], source["artifact"]
        run_id = producer["run_id"]
        run = {"id": run_id, "run_attempt": 2, "workflow_id": run_id + 100,
               "path": producer["workflow_path"], "created_at": "2026-10-07T10:00:00Z",
               "event": producer["event"], "head_sha": producer["api_head_sha"], "head_branch": "master",
               "head_repository": {"full_name": api.repository}, "status": "completed", "conclusion": "success",
               "referenced_workflows": [{"path": f"{plan['identity']['kit']['repository']}/.github/workflows/{'build' if run_id == 42 else 'packaged-e2e'}.yml@{plan['identity']['kit']['sha']}",
                                          "sha": plan["identity"]["kit"]["sha"]}]}
        api.add_run(run)
        runs[run_id] = run
        record = {"id": selected["id"], "name": selected["name"], "size_in_bytes": selected["size"],
                  "digest": selected["digest"], "created_at": selected["created_at"],
                  "expires_at": selected["expires_at"], "expired": False,
                  "workflow_run": {"id": run_id, "head_branch": "master", "head_sha": producer["api_head_sha"]}}
        api.add_artifact(record, data if source is descriptor else b"unused-native-payload")
        records[selected["id"]] = record
    return plan, api, document, descriptor, jobs, raw, data, records, runs


class GateTransportTests(unittest.TestCase):
    def call(self, fixture, parent):
        return transport.download_gate_receipt(fixture[1], descriptor=fixture[3], plan=fixture[0],
            gate=fixture[2]["gate"], workflow_path=fixture[3]["producer"]["workflow_path"],
            build_workflow_path=".github/workflows/build-gate.yml", temporary_root=parent)

    def seams(self, fixture):
        return (patch.object(transport, "extract", return_value=[grammar.CI_GATE_NAME]),
                patch.object(transport, "read_child_file", return_value=fixture[5]))

    def test_both_full_receipts_read_by_numeric_id_without_mutation_or_residue(self):
        for gate in ("build", "packaged"):
            fixture = gate_transport_fixture(gate)
            extract, read = self.seams(fixture)
            before = copy.deepcopy(fixture[2:4])
            with tempfile.TemporaryDirectory() as directory, extract as extraction, read, \
                    patch.object(fixture[1], "download", wraps=fixture[1].download) as download:
                self.assertEqual(self.call(fixture, Path(directory)), fixture[2])
                self.assertEqual(list(Path(directory).iterdir()), [])
            download.assert_called_once_with(f"/repos/example/mod/actions/artifacts/200/zip",
                                             max_bytes=fixture[3]["artifact"]["size"])
            bounds = extraction.call_args.args[2]
            self.assertEqual((bounds.max_entries, bounds.max_total_bytes, bounds.max_entry_bytes),
                             (1, limits.MAX_CI_RECORD_BYTES, limits.MAX_CI_RECORD_BYTES))
            self.assertEqual(fixture[2:4], before)
            self.assertEqual(fixture[1].mutations, [])

    def test_wrong_gate_kind_plan_and_enrollment_reject_before_download(self):
        for mutation in ("kind", "unit", "plan", "workflow", "build-workflow"):
            fixture = gate_transport_fixture()
            if mutation == "kind":
                fixture[3]["artifact"]["name"] = grammar.ci_artifact_name("build", 42, 2)
            elif mutation == "unit":
                fixture[3]["artifact"]["name"] = grammar.ci_artifact_name("tested", 42, 2, "packaged")
            elif mutation == "plan":
                fixture[3]["plan_sha256"] = "f" * 64
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                transport.download_gate_receipt(fixture[1], descriptor=fixture[3], plan=fixture[0], gate="build",
                    workflow_path=".github/workflows/foreign.yml" if mutation == "workflow" else ".github/workflows/build-gate.yml",
                    build_workflow_path=".github/workflows/foreign.yml" if mutation == "build-workflow" else ".github/workflows/build-gate.yml",
                    temporary_root=Path(directory))
            download.assert_not_called()

    def test_new_attempt_wrong_head_failed_run_and_wrong_kit_reject_before_fetch(self):
        for gate in ("build", "packaged"):
            for mutation in ("attempt", "head", "failed", "kit"):
                fixture = gate_transport_fixture(gate)
                run = fixture[8][fixture[3]["producer"]["run_id"]]
                if mutation == "attempt":
                    run["run_attempt"] = 3
                elif mutation == "head":
                    run["head_sha"] = "f" * 40
                elif mutation == "failed":
                    run["conclusion"] = "failure"
                else:
                    run["referenced_workflows"][0]["sha"] = "f" * 40
                fixture[1].add_run(run)
                with self.subTest(gate=gate, mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                        patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                    self.call(fixture, Path(directory))
                download.assert_not_called()

    def test_record_immutable_metadata_expiry_and_owner_are_checked_before_fetch(self):
        for mutation in ("name", "digest", "size", "expiry", "owner"):
            fixture = gate_transport_fixture()
            record = fixture[7][200]
            if mutation == "name":
                record["name"] = grammar.ci_artifact_name("tested", 42, 2, "packaged")
            elif mutation == "digest":
                record["digest"] = "sha256:" + "f" * 64
            elif mutation == "size":
                record["size_in_bytes"] += 1
            elif mutation == "expiry":
                record["expired"] = True
            else:
                record["workflow_run"]["id"] = 43
            fixture[1].add_artifact(record, fixture[6])
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory))
            download.assert_not_called()

    def test_download_size_and_digest_reject_before_extraction(self):
        for data in (b"wrong", b"x" * len(gate_transport_fixture()[6])):
            fixture = gate_transport_fixture()
            with tempfile.TemporaryDirectory() as directory, patch.object(fixture[1], "download", return_value=data), \
                    patch.object(transport, "extract") as extraction, self.assertRaisesRegex(MbError, "ZIP length or digest"):
                self.call(fixture, Path(directory))
            extraction.assert_not_called()

    def test_wrong_extra_or_nested_record_paths_reject(self):
        for files in (["other.json"], [grammar.CI_GATE_NAME, "extra.json"], ["nested/ci-gate.json"]):
            fixture = gate_transport_fixture()
            with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract", return_value=files), \
                    patch.object(transport, "read_child_file") as read:
                with self.assertRaisesRegex(MbError, "fixed root filename"):
                    self.call(fixture, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])
            read.assert_not_called()

    def test_noncanonical_duplicate_unknown_and_wrong_kind_json_reject(self):
        document = gate_transport_fixture()[2]
        for raw in (canonical_json(document) + b"\n", canonical_json(document).replace(b'"mode":"full"', b'"mode":"full","mode":"full"'),
                    canonical_json({**document, "extra": True}), canonical_json({**document, "kind": "mod-base.ci.reuse"})):
            fixture = gate_transport_fixture(raw=raw)
            extract, read = self.seams(fixture)
            with tempfile.TemporaryDirectory() as directory, extract, read:
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_every_source_artifact_remains_available_and_matches_metadata(self):
        for source_id in (100, 101, 102):
            for mutation in ("expiry", "digest", "owner"):
                fixture = gate_transport_fixture("packaged")
                record = fixture[7][source_id]
                if mutation == "expiry":
                    record["expired"] = True
                elif mutation == "digest":
                    record["digest"] = "sha256:" + "f" * 64
                else:
                    record["workflow_run"]["id"] = 999
                fixture[1].add_artifact(record, b"unused")
                extract, read = self.seams(fixture)
                with self.subTest(source=source_id, mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                        extract, read, self.assertRaises(MbError):
                    self.call(fixture, Path(directory))

    def test_owning_build_enrollment_and_latest_attempt_are_independent(self):
        for mutation in ("enrollment", "attempt"):
            fixture = gate_transport_fixture("packaged")
            if mutation == "enrollment":
                owner = fixture[2]["owning_build"]["producer"]
                owner.update(workflow_path=".github/workflows/foreign.yml",
                             workflow_ref="example/mod/.github/workflows/foreign.yml@refs/heads/master")
                fixture = gate_transport_fixture("packaged", raw=canonical_json(fixture[2]))
            else:
                run = fixture[8][42]
                run["run_attempt"] = 3
                fixture[1].add_run(run)
            extract, read = self.seams(fixture)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, extract, read, self.assertRaises(MbError):
                self.call(fixture, Path(directory))

    def test_changed_head_record_attempt_or_source_expiry_after_download_rejects(self):
        for mutation in ("head", "attempt", "source"):
            fixture = gate_transport_fixture()
            original = fixture[1].download
            def downloading(path, **kwargs):
                data = original(path, **kwargs)
                if mutation == "head":
                    fixture[1].set_branch("master", "f" * 40, "e" * 40)
                elif mutation == "attempt":
                    run = fixture[8][42]
                    run["run_attempt"] = 3
                    fixture[1].add_run(run)
                else:
                    record = fixture[7][100]
                    record["expired"] = True
                    fixture[1].add_artifact(record, b"unused")
                return data
            extract, read = self.seams(fixture)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, extract, read, \
                    patch.object(fixture[1], "download", side_effect=downloading), self.assertRaises(MbError):
                self.call(fixture, Path(directory))

    def test_real_gate_timeline_is_required_before_return(self):
        fixture = gate_transport_fixture()
        gate = next(job for job in fixture[4]["build"] if job["steps"] and "Verify complete" in job["name"])
        gate["steps"][0]["started_at"] = "2026-10-07T10:01:00Z"
        fixture[1].add_jobs(42, 2, fixture[4]["build"])
        extract, read = self.seams(fixture)
        with tempfile.TemporaryDirectory() as directory, extract, read, self.assertRaises(MbError):
            self.call(fixture, Path(directory))

    def test_extraction_and_temporary_creation_failures_are_bounded_and_cleanup(self):
        fixture = gate_transport_fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, "extract", side_effect=MbError("unsafe ZIP")):
            with self.assertRaisesRegex(MbError, "unsafe ZIP"):
                self.call(fixture, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "missing")
