"""The runtime envelope protected code writes for one lane, from the plan and a real directory.

The directory is what a lane's hook left: its native results and no kit document. The account,
the ownership handover and the source and Build checks around the copy need root and are exercised
by ``LinuxCandidateCommandTests`` in ``tests/ci_linux_worker.py``.
"""

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import runtime_freeze
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.build_ci.worker import WorkerAccount
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_descriptor, ci_envelope, ci_plan, ci_run_producer, ci_staged_plan

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
RESULTS = {
    "lanes/lane-a/result.json": (b'{"status":"pass"}\n', "native-report"),
    "lanes/lane-a/logs/client.log": (b"client started\n", "runtime-log"),
    "lanes/lane-a/logs/empty.log": (b"", "runtime-log"),
    "lanes/lane-a/screenshots/Title Screen.png": (PNG, "screenshot"),
    "lanes/lane-a/run/crash-reports/crash-2026-10-08_10.00.00-client.txt": (b"", "crash-report"),
    "lanes/lane-a/run/options.txt": (b"fov:70\n", "runtime-log"),
}


class RuntimeRoleTests(unittest.TestCase):
    def test_the_role_follows_from_the_name_alone(self):
        roles = {"result.json": "native-report", "lanes/a/REPORT.JSON": "native-report",
                 "shot.png": "screenshot", "lanes/a/screenshots/Title.PNG": "screenshot",
                 "latest.log": "runtime-log", "logs/debug.log.gz": "runtime-log", "options.txt": "runtime-log",
                 "notes": "runtime-log", "report.json.bak": "runtime-log", "png": "runtime-log",
                 "crash-reports/crash.txt": "crash-report", "run/crash-reports/crash.json": "crash-report",
                 "run/crash-reports/dump/frame.png": "crash-report",
                 # A file that is merely named like the directory is no crash report.
                 "run/crash-reports": "runtime-log", "run/crash-reports.json": "native-report"}
        for path, role in roles.items():
            with self.subTest(path=path):
                self.assertEqual(runtime_freeze.runtime_role(path), role)


class LaneSealTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.output = self.base / "export", self.base / "sealed"
        self.plan = ci_plan()
        self.producer = {key: value for key, value in ci_run_producer(self.plan, "packaged").items()
                         if key != "upload_window"}
        self.owning_build = ci_descriptor()
        for name, (data, _) in RESULTS.items():
            self.put(name, data)

    def put(self, name, data):
        path = self.root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def seal(self, lane_id="lane-a", **changes):
        arguments = {"producer": self.producer, "owning_build": self.owning_build, **changes}
        return runtime_freeze.seal_lane_export(self.root, self.output, plan=self.plan, lane_id=lane_id, **arguments)

    def published(self):
        return sorted(path.name for path in self.base.iterdir())

    def test_the_envelope_names_the_lane_of_the_plan_and_every_file_found_with_its_derived_role(self):
        envelope = self.seal()
        self.assertEqual(validate_runtime_envelope(copy.deepcopy(envelope), plan=self.plan), envelope)
        lane = self.plan["lanes"][0]
        self.assertEqual({key: value for key, value in envelope.items() if key != "files"}, {
            "kind": "mod-base.ci.runtime-envelope", "schema_version": 1, "identity": self.plan["identity"],
            "plan_sha256": self.plan["plan_sha256"], "profile": self.plan["profile"], "producer": self.producer,
            "scope": "lane", "lane_id": "lane-a", "owning_build": self.owning_build,
            "lanes": [{"id": "lane-a", "native_contract_sha256": lane["native_contract_sha256"]}]})
        self.assertEqual(envelope["files"], [
            {"path": name, "lane_id": "lane-a", "role": role, "size": len(data),
             "sha256": hashlib.sha256(data).hexdigest()} for name, (data, role) in sorted(RESULTS.items())])
        # An independent copy, empty logs included, with the one document the kit wrote.
        self.assertEqual(sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob("*")
                                if path.is_file()), sorted([*RESULTS, grammar.CI_RUNTIME_ENVELOPE_NAME]))
        self.assertEqual((self.output / grammar.CI_RUNTIME_ENVELOPE_NAME).read_bytes(), canonical_json(envelope))
        self.assertEqual(verify_runtime_export(self.output, plan=self.plan), envelope)
        for name, (data, _) in RESULTS.items():
            self.assertEqual((self.output / name).read_bytes(), data)
            self.assertNotEqual(os.stat(self.output / name).st_ino, os.stat(self.root / name).st_ino)
        self.assertFalse((self.root / grammar.CI_RUNTIME_ENVELOPE_NAME).exists())
        self.assertEqual(self.published(), ["export", "sealed"])

    def test_the_envelope_is_a_function_of_the_inventory_the_plan_and_the_selected_build(self):
        records = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                   for name, (data, _) in RESULTS.items()]
        arguments = dict(plan=self.plan, lane_id="lane-a", producer=self.producer, owning_build=self.owning_build)
        envelope = runtime_freeze.lane_envelope(records, **arguments)
        self.assertEqual(envelope, self.seal())
        self.assertEqual(runtime_freeze.lane_envelope(records[::-1], **arguments), envelope)
        self.assertIsNot(envelope["owning_build"], self.owning_build)
        staged = ci_staged_plan()  # Three lanes: the envelope of one names that lane alone.
        owner = ci_descriptor()
        owner["plan_sha256"] = staged["plan_sha256"]
        producer = {key: value for key, value in ci_run_producer(staged, "packaged").items() if key != "upload_window"}
        moved = [{**record, "path": record["path"].replace("lane-a", "lane-b")} for record in records]
        other = runtime_freeze.lane_envelope(moved, plan=staged, lane_id="lane-b", producer=producer, owning_build=owner)
        self.assertEqual((other["lane_id"], [lane["id"] for lane in other["lanes"]],
                          {file["lane_id"] for file in other["files"]}), ("lane-b", ["lane-b"], {"lane-b"}))

    def test_results_no_lane_may_leave_are_refused_and_nothing_is_published(self):
        report = "lanes/lane-a/result.json"

        def linked():
            (self.root / report).unlink()
            (self.root / report).symlink_to(self.root / "lanes/lane-a/logs/client.log")

        def emptied():
            for path in sorted(self.root.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()

        mutations = {
            "nothing at all": (emptied, "files"),
            "an empty report": (lambda: self.put(report, b""), "empty report/image data"),
            "an empty screenshot": (lambda: self.put("lanes/lane-a/screenshots/blank.png", b""),
                                    "empty report/image data"),
            "a runtime envelope written by the hook": (
                lambda: self.put(grammar.CI_RUNTIME_ENVELOPE_NAME, b"{}"), "its own envelope"),
            "a result that is a symlink": (linked, "symlink"),
            "a result with a second name": (lambda: os.link(self.root / report, self.base / "alias"), "links"),
            "a pipe": (lambda: os.mkfifo(self.root / "lanes/lane-a/logs/pipe"), "special file"),
            "a hidden file": (lambda: self.put("lanes/lane-a/.hidden", b"x"), "unsafe or aliased path"),
            "Git internals": (lambda: self.put("lanes/lane-a/.git/config", b"x"), "unsafe or aliased path"),
            "a case alias": (lambda: self.put("lanes/lane-a/Result.json", b"{}"), "unsafe or aliased path"),
        }
        for name, (mutate, message) in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                with self.assertRaisesRegex(MbError, message):
                    self.seal()
                self.assertFalse(self.output.exists())
                self.assertEqual(sorted(set(self.published()) - {"alias"}), ["export"])  # No stage is left behind.

    def test_a_lane_outside_the_plan_an_unbound_build_and_a_foreign_producer_are_refused(self):
        other_plan = ci_staged_plan()
        target = ci_descriptor("target", unit_id="target-a")
        for changes in ({"lane_id": "lane-b"}, {"lane_id": "target-a"},
                        {"owning_build": {**self.owning_build, "plan_sha256": other_plan["plan_sha256"]}},
                        {"owning_build": target}, {"owning_build": ci_envelope()},
                        {"producer": {**self.producer, "api_head_sha": "e" * 40}},
                        {"producer": {**self.producer, "event": "push"}},
                        {"producer": {**self.producer, "upload_window": {}}}):
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                self.seal(**changes)
            self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaisesRegex(MbError, "refusing to replace existing output"):
            self.seal()

    def test_every_role_keeps_its_own_size_bound_and_one_lane_its_caps(self):
        for bound, value in (("MAX_CI_REPORT_BYTES", 8), ("MAX_CI_PNG_BYTES", 8), ("MAX_CI_LOG_BYTES", 8),
                             ("MAX_CI_RUNTIME_FILES", 5), ("MAX_CI_RUNTIME_BYTES", 32), ("MAX_CI_RUNTIME_ENTRIES", 6),
                             ("MAX_CI_RUNTIME_ENVELOPE_BYTES", 64)):
            with self.subTest(bound=bound), patch.object(limits, bound, value), self.assertRaises(MbError):
                self.seal()
            self.assertFalse(self.output.exists())
        self.assertEqual(self.seal()["lane_id"], "lane-a")

    def test_the_root_only_freeze_refuses_an_unprivileged_caller_before_it_looks_at_anything(self):
        if os.geteuid() == 0:
            self.skipTest("this case needs an unprivileged user")
        boundary = HostBoundary("/home/runner", os.getuid(), os.getgid(), 1, 10, 0o755)
        candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/mod-base-sandbox-boundary/mod-base-worker/candidate-home")
        with patch.object(runtime_freeze, "terminate_worker") as terminate, \
                self.assertRaisesRegex(MbError, "requires protected root setup"):
            runtime_freeze.freeze_runtime_export(
                boundary=boundary, candidate=candidate, inventory=(), generated_roots=(), plan=self.plan,
                build=ci_envelope(), owning_build=self.owning_build, lane_id="lane-a", producer=self.producer)
        terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
