"""Tested records are read by numeric ID from real archives against a fake GitHub API.

Only the API is faked; the record ZIP is extracted and read by the kit's own bounded reader in a
temporary directory (Linux).
"""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_graph_jobs
from tests.test_ci_gate_timeline import GATES, PACKAGED_GATE, gate_world
from tests.test_ci_protocol import GRAPHS
from tests.test_ci_transport import ASSEMBLE, GATE, after_download, record_archive


def reseal(world, gate, *, raw=None, filename=grammar.CI_GATE_NAME):
    """Publish the gate's record again: after its document changed, or as hostile bytes."""

    data = record_archive(world.documents[gate], filename, raw)
    seal = world.seals[gate]
    seal["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
    world.archives[seal["artifact"]["id"]] = data
    world.set_artifact(seal["artifact"]["id"], size_in_bytes=len(data), digest=seal["artifact"]["digest"])
    return world


class GateTransportTests(unittest.TestCase):
    def call(self, world, name, parent, **changes):
        arguments = {"descriptor": world.seals[name], "plan": world.plan, "gate": name,
                     "temporary_root": parent, **changes}
        return transport.download_gate_receipt(world.api, **arguments)

    def refused(self, world, gate, message=""):
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as downloads:
            with self.assertRaisesRegex(MbError, message):
                self.call(world, gate, Path(directory))
            downloads.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_every_sealing_mode_is_read_by_numeric_id_without_mutation_or_residue(self):
        # Requests: the subject (a pull request 3 + its commit, a push 2 + its commit), the gate's run
        # and jobs, its record (1 + download 2) and its sources, plus a separate Build run when one
        # was consumed; then the subject, each run and each artifact read once more.
        budget = {("build", "full", False): 16, ("packaged", "pull-request", False): 21,
                  ("build", "full", True): 14, ("packaged", "selected", True): 19,
                  ("build", "rebuilt", True): 14, ("packaged", "rebuilt", True): 14}
        for gate, mode, push in GATES:
            world = gate_world(gate, mode, push=push)
            before = copy.deepcopy((world.plan, world.documents[gate], world.seals[gate]))
            seal = world.seals[gate]["artifact"]
            with self.subTest(gate=gate, mode=mode, push=push), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download", wraps=world.api.download) as downloads:
                self.assertEqual(self.call(world, gate, Path(directory)), world.documents[gate])
                self.assertEqual(list(Path(directory).iterdir()), [])
                downloads.assert_called_once_with(f"/repos/example/mod/actions/artifacts/{seal['id']}/zip",
                                                  max_bytes=seal["size"])
                self.assertEqual((world.plan, world.documents[gate], world.seals[gate]), before)
                self.assertEqual(world.api.mutations, [])
                self.assertEqual(world.api.request_count, budget[gate, mode, push])
                self.assertLess(world.api.request_count, 60)

    def test_source_availability_costs_one_listing_per_run_whatever_the_lane_count(self):
        world = gate_world("packaged")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "get_json", wraps=world.api.get_json) as reads:
            self.call(world, "packaged", Path(directory))
        paths = [call.args[0] for call in reads.call_args_list]
        self.assertEqual(sum(path.endswith("/actions/runs/43/artifacts") for path in paths), 2)
        self.assertEqual(sum(path.endswith("/actions/artifacts/101") or path.endswith("/actions/artifacts/102")
                             for path in paths), 0)
        self.assertEqual(sum(path.endswith("/attempts/2/jobs") for path in paths), 2)  # each run's jobs once
        self.assertEqual(sum("/git/commits/" in path for path in paths), 1)

    def test_wrong_gate_kind_plan_and_arguments_reject_before_any_read(self):
        world = gate_world("build")
        packaged = gate_world("packaged")
        cases = [{"gate": "packaged"}, {"gate": "reuse"}, {"gate": None},
                 {"descriptor": world.documents["build"]["artifacts"][0]},
                 {"descriptor": {**world.seals["build"], "plan_sha256": "f" * 64}},
                 {"descriptor": packaged.seals["packaged"]}, {"plan": {**world.plan, "plan_sha256": "f" * 64}},
                 {"descriptor": {**world.seals["build"], "artifact": {**world.seals["build"]["artifact"],
                                                                     "digest": "invalid"}}}]
        for index, changes in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, self.assertRaises(MbError):
                self.call(world, "build", Path(directory), **changes)
            reads.assert_not_called()

    def test_run_identity_attempt_result_and_references_reject_before_fetch(self):
        def other_controller(run):
            entry = run["referenced_workflows"][0]
            entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)

        def other_kit(run):
            for entry in run["referenced_workflows"][1:]:
                entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)

        mutations = {"attempt": lambda run: run.update(run_attempt=3),
                     "base head": lambda run: run.update(head_sha="2" * 40, head_branch="master"),
                     "failed": lambda run: run.update(conclusion="failure"),
                     "running": lambda run: run.update(status="in_progress", conclusion=None),
                     "event": lambda run: run.update(event="push"),
                     "controller": other_controller, "kit": other_kit,
                     "unreferenced": lambda run: run.update(referenced_workflows=[])}
        for gate in ("build", "packaged"):
            for name, mutate in mutations.items():
                world = gate_world(gate)
                run_id = world.seals[gate]["producer"]["run_id"]
                mutate(world.runs[run_id])
                world.api.add_run(world.runs[run_id])
                with self.subTest(gate=gate, mutation=name):
                    self.refused(world, gate)

    def test_record_metadata_expiry_owner_and_head_reject_before_fetch(self):
        for changes in ({"name": grammar.ci_artifact_name("tested", 42, 2, "packaged")},
                        {"digest": "sha256:" + "f" * 64}, {"size_in_bytes": 7}, {"expired": True},
                        {"created_at": "2026-10-07T10:05:31Z"}, {"workflow_run": {"id": 43}},
                        {"workflow_run": {"head_sha": "2" * 40}}, {"workflow_run": {"head_branch": "master"}}):
            world = gate_world("build")
            world.set_artifact(200, **copy.deepcopy(changes))
            with self.subTest(changes=changes):
                self.refused(world, "build")

    def test_gate_graph_seal_and_upload_window_reject_before_fetch(self):
        for listing in ("build-deferred", "build-reuse", "build-attest-only", "build-full-extra-job",
                        "build-full-wrong-conclusion"):
            world = gate_world("build")
            world.set_jobs(42, ci_graph_jobs(listing))
            with self.subTest(listing=listing):
                self.refused(world, "build")
        world = gate_world("build")
        world.job(42, GATE)["steps"][1]["conclusion"] = "failure"
        world.set_jobs(42, world.jobs[42])
        self.refused(world, "build")
        world = gate_world("build")
        world.seals["build"]["producer"]["upload_window"]["started_at"] = "2026-10-07T10:04:59Z"
        self.refused(world, "build", "wrong upload window")
        # A digest that is the graph of no admissible mode is refused before the API is asked anything.
        for digest in ("f" * 64, GRAPHS["build", "deferred"], GRAPHS["build", "reuse"],
                       GRAPHS["packaged", "pull-request"]):
            world = gate_world("build")
            world.seals["build"]["producer"]["graph_sha256"] = digest
            with self.subTest(digest=digest[:8]), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, \
                    self.assertRaisesRegex(MbError, "admissible gate mode"):
                self.call(world, "build", Path(directory))
            reads.assert_not_called()

    def test_download_size_and_digest_reject_before_extraction(self):
        for data in (b"wrong", None):
            world = gate_world("build")
            world.archives[200] = bytes(len(world.archives[200])) if data is None else data
            world.api.add_artifact(world.records[200], world.archives[200])
            with self.subTest(size=len(world.archives[200])), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(MbError, "download length or SHA-256 differs|exceeds its"):
                    self.call(world, "build", Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_wrong_extra_or_nested_record_files_reject(self):
        import io
        import zipfile

        def archive(entries):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as package:
                for name, data in entries.items():
                    package.writestr(name, data)
            return stream.getvalue()

        world = gate_world("build")
        raw = canonical_json(world.documents["build"])
        for entries in ({"other.json": raw}, {grammar.CI_GATE_NAME: raw, "extra.json": b"{}"},
                        {"nested/" + grammar.CI_GATE_NAME: raw}, {grammar.CI_GATE_NAME + ".txt": raw}):
            world = gate_world("build")
            data = archive(entries)
            seal = world.seals["build"]
            seal["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
            world.archives[200] = data
            world.set_artifact(200, size_in_bytes=len(data), digest=seal["artifact"]["digest"])
            with self.subTest(entries=sorted(entries)), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(MbError):
                    self.call(world, "build", Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_noncanonical_duplicate_unknown_and_other_records_reject(self):
        document = gate_world("build").documents["build"]
        packaged = gate_world("packaged").documents["packaged"]
        raws = [canonical_json(document) + b"\n",
                canonical_json(document).replace(b'"mode":"full"', b'"mode":"full","mode":"full"'),
                canonical_json({**document, "extra": True}), canonical_json({**document, "kind": "mod-base.ci.reuse"}),
                canonical_json({**document, "mode": "rebuilt"}), canonical_json({**document, "mode": "deferred"}),
                canonical_json(packaged), b"{}", b"not JSON"]
        for index, raw in enumerate(raws):
            world = reseal(gate_world("build"), "build", raw=raw)
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(MbError):
                    self.call(world, "build", Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_record_of_another_run_attempt_or_plan_is_not_this_seal(self):
        mutations = {"run": lambda d: [item["producer"].update(run_id=41) for item in (d, *d["artifacts"])],
                     "graph": lambda d: [item["producer"].update(graph_sha256="f" * 64)
                                         for item in (d, *d["artifacts"])],
                     "receipts": lambda d: d["native_receipts"].clear(),
                     "contract": lambda d: d["native_receipts"][0].update(native_contract_sha256="f" * 64)}
        for name, mutate in mutations.items():
            world = gate_world("build")
            mutate(world.documents["build"])
            if name == "run":
                world.documents["build"]["artifacts"][0]["artifact"]["name"] = grammar.ci_artifact_name("build", 41, 2)
            reseal(world, "build")
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(world, "build", Path(directory))

    def test_every_source_artifact_remains_available_and_matches_its_metadata(self):
        for artifact_id in (100, 101, 102):
            for changes in ({"expired": True}, {"digest": "sha256:" + "f" * 64}, {"workflow_run": {"id": 999}},
                            {"workflow_run": {"head_sha": "2" * 40}}, {"size_in_bytes": 3}):
                world = gate_world("packaged")
                world.set_artifact(artifact_id, **copy.deepcopy(changes))
                with self.subTest(artifact_id=artifact_id, changes=changes), \
                        tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                    self.call(world, "packaged", Path(directory))
        # A lane artifact that its run no longer lists is gone, whatever else the listing holds.
        world = gate_world("packaged")
        world.set_artifact(101, workflow_run={"id": 999})
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(MbError, "no longer listed"):
            self.call(world, "packaged", Path(directory))

    def test_owning_build_run_is_authenticated_as_its_own_completed_full_run(self):
        mutations = {"attempt": lambda world: world.set_run(42, run_attempt=3),
                     "failed": lambda world: world.set_run(42, conclusion="failure"),
                     "base head": lambda world: world.set_run(42, head_sha="2" * 40),
                     "deferred": lambda world: world.set_jobs(42, ci_graph_jobs("build-deferred")),
                     "unsealed": lambda world: (world.job(42, ASSEMBLE)["steps"].pop(2),
                                                world.set_jobs(42, world.jobs[42]))}
        for name, mutate in mutations.items():
            world = gate_world("packaged")
            mutate(world)
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(world, "packaged", Path(directory))
        # The record names another Build than the one this subject's Build caller produced.
        world = gate_world("packaged")
        world.documents["packaged"]["owning_build"]["producer"].update(run_id=44)
        world.documents["packaged"]["owning_build"]["artifact"]["name"] = grammar.ci_artifact_name("build", 44, 2)
        reseal(world, "packaged")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "packaged", Path(directory))

    def test_changes_after_the_record_download_reject_before_it_is_returned(self):
        changes = {"head": lambda world: world.api.set_branch("master", "f" * 40, "e" * 40),
                   "draft": lambda world: world.api.add_response("/repos/example/mod/pulls/7",
                                                                 {**world.pr, "draft": True}),
                   "attempt": lambda world: world.set_run(43, run_attempt=3, status="queued", conclusion=None),
                   "build attempt": lambda world: world.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "record": lambda world: world.set_artifact(201, expired=True),
                   "lane": lambda world: world.set_artifact(101, expired=True),
                   "build": lambda world: world.set_artifact(100, expired=True)}
        for name, change in changes.items():
            world = gate_world("packaged")
            original = world.api.paginate
            fired = []

            def paginate(path, **arguments):
                # The run's artifact listing is the last read of the start phase.
                rows = original(path, **arguments)
                if path.endswith("/actions/runs/43/artifacts") and not fired:
                    fired.append(name)
                    change(world)
                return rows

            with self.subTest(change=name), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "paginate", side_effect=paginate):
                with self.assertRaisesRegex(MbError, "changed between the start of the command|moved|ready"):
                    self.call(world, "packaged", Path(directory))
                self.assertEqual(fired, [name])
                self.assertEqual(list(Path(directory).iterdir()), [])
        world = gate_world("build")
        with tempfile.TemporaryDirectory() as directory, \
                after_download(world.api, lambda: world.set_artifact(100, expired=True)), self.assertRaises(MbError):
            self.call(world, "build", Path(directory))

    def test_real_gate_timeline_is_required_before_return(self):
        world = gate_world("build")
        world.job(42, ASSEMBLE)["completed_at"] = "2026-10-07T10:04:01Z"
        world.set_jobs(42, world.jobs[42])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "build", Path(directory))
        world = gate_world("packaged")
        world.job(43, PACKAGED_GATE)["steps"][1]["started_at"] = "2026-10-07T02:10:00.000-08:00"
        world.set_jobs(43, world.jobs[43])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "packaged", Path(directory))

    def test_record_size_is_bounded_and_missing_temporary_root_is_an_error(self):
        world = gate_world("build")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "build", Path(directory) / "missing")
        world = gate_world("build")
        world.seals["build"]["artifact"]["size"] = limits.MAX_CI_RECORD_BYTES + 1
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as reads, \
                self.assertRaises(MbError):
            self.call(world, "build", Path(directory))
        reads.assert_not_called()

    def test_caller_documents_are_copied_on_entry(self):
        world = gate_world("build")
        expected = copy.deepcopy(world.documents["build"])
        with tempfile.TemporaryDirectory() as directory, \
                after_download(world.api, lambda: (world.plan.clear(), world.seals["build"].clear())):
            self.assertEqual(transport.download_gate_receipt(world.api, descriptor=world.seals["build"],
                                                             plan=world.plan, gate="build",
                                                             temporary_root=Path(directory)), expected)


if __name__ == "__main__":
    unittest.main()
