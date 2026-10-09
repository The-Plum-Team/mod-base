"""The worker lifecycle below the accounts: job binding, state records, arguments, candidate files.

Real files, Git and processes in temporary directories; only the GitHub API is a fake. What needs
accounts, sudo and root operations is ``LinuxLifecycleCommandTests`` in ``tests/ci_linux_worker.py``.
No test here passes a hosted runner layout to ``ci worker-prepare``: on a machine that looks like a
runner the command would really fence it.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from mod_base import runtime
from mod_base.build_ci import adapter, controller, identity, inputs, lifecycle, planning
from mod_base.build_ci.config import BUILD_CONFIG_PATH, load_build_config
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.protocol import subject_of
from mod_base.build_ci.worker import WorkerAccount, WorkerError
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h
from tests.helpers import ci_plan_inputs

SUBJECT_REQUESTS = 4
#: `ci plan` without a checkout: the tested tree and one blob per candidate file of the fixture.
PLAN_REQUESTS = 4
SOURCE_NAMES = ["inventory", "scenario-contract", "gradle-properties"]
HOSTED = {"RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_WORKSPACE": "/home/runner/work/mod/mod",
          "RUNNER_TEMP": "/home/runner/work/_temp"}


def candidate_sources(mod: Path) -> dict[str, bytes]:
    """The bytes of every candidate file the protected config of ``mod`` names, by staged name."""

    config = load_build_config(mod, repository=h.REPOSITORY)
    return {name: (mod / path).read_bytes() for name, path in adapter.plan_sources(config.data).items()}


class JobCase(unittest.TestCase):
    """A job of the fixture pull request on a copy of the synthetic mod, after ``ci subject``."""

    faults = None

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.mod = h.materialize(self.temporary / "mod", faults=self.faults)
        self.state = self.temporary / "state"
        self.output = self.temporary / "github-output"
        self.api, self.pull = h.github()
        self.environment = h.environment()
        self.commands = fixture.Commands(self.mod, self.state, self.api, self.environment)

    def begin(self) -> lifecycle.Job:
        self.commands.subject(self.output)
        return self.job()

    def job(self, **environment: str) -> lifecycle.Job:
        return lifecycle.open_job(runtime.build_invocation(self.mod, None, {**self.environment, **environment}),
                                  self.state)

    def records(self) -> set[str]:
        return {path.name for path in self.state.iterdir()}


class OpenJobTests(JobCase):
    def test_a_job_is_the_subject_the_verified_checkout_and_the_executing_kit(self) -> None:
        job = self.begin()
        record = identity.read_subject(self.state)
        self.assertEqual((job.state, job.record, job.subject), (self.state, record, record["subject"]))
        self.assertEqual(job.config, load_build_config(self.mod, repository=h.REPOSITORY))
        self.assertEqual((job.run_id, job.run_attempt), (42, 1))
        self.assertEqual((job.kit_root, job.kit_digest), (runtime.kit_root(), record["subject"]["kit"]["tree_digest"]))
        # The adapter closure is the bytes of the checkout, bound to the controller of the subject.
        self.assertEqual((job.sources.controller_sha, job.sources.controller_tree),
                         (h.CONTROLLER_SHA, h.CONTROLLER_TREE))
        self.assertEqual({file.path: file.data for file in (job.sources.config, *job.sources.files)},
                         {name: (self.mod / name).read_bytes() for name in
                          (BUILD_CONFIG_PATH, *(entry["path"] for entry in job.config.data["adapter"]["files"]))})
        self.assertEqual(controller._validate_sources(job.sources, job.subject), job.config.data)
        # Rule 5: nothing of it was read from the API.
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))

    def test_checkout_sources_materialize_into_the_copy_the_validator_runs(self) -> None:
        job = self.begin()
        output = self.temporary / "controller"
        self.assertEqual(controller.materialize_controller_sources(output, sources=job.sources, identity=job.subject),
                         job.config.data)
        self.assertEqual({path.relative_to(output).as_posix(): path.read_bytes()
                          for path in output.rglob("*") if path.is_file()},
                         {file.path: file.data for file in (job.sources.config, *job.sources.files)})
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in output.rglob("*") if path.is_file()))
        (output / "scripts/ci/policy_suite.py").write_bytes(b"changed\n")
        with self.assertRaises(MbError):
            controller.verify_controller_source_copy(output, sources=job.sources, identity=job.subject)
        for forged in (replace(job.sources, controller_sha="e" * 40),
                       replace(job.sources, files=job.sources.files[:-1]),
                       replace(job.sources, config=replace(job.sources.config, data=b"{}"))):
            with self.subTest(forged=forged.controller_sha), self.assertRaises(MbError):
                controller.materialize_controller_sources(self.temporary / "other", sources=forged,
                                                          identity=job.subject)
        self.assertFalse((self.temporary / "other").exists())
        with self.assertRaises(MbError):
            controller.checkout_controller_sources(job.config.data, controller_sha=h.CONTROLLER_SHA,
                                                   controller_tree=h.CONTROLLER_TREE)

    def test_state_of_another_repository_commit_kit_or_run_is_refused(self) -> None:
        self.begin()
        for change in ({"GITHUB_SHA": "e" * 40}, {"MOD_BASE_KIT_SHA": "5" * 40}, {"GITHUB_RUN_ID": ""},
                       {"GITHUB_RUN_ID": "0"}, {"GITHUB_RUN_ID": "4x2"}, {"GITHUB_RUN_ATTEMPT": "-1"},
                       {"GITHUB_RUN_ATTEMPT": str(limits.MAX_RUN_ATTEMPT + 1)}):
            with self.subTest(change=change), self.assertRaises(MbError):
                self.job(**change)
        with mock.patch.object(lifecycle, "kit_tree_digest", return_value="sha256:" + "0" * 64), \
                self.assertRaisesRegex(MbError, "executing kit"):
            self.job()

    def test_a_changed_protected_source_or_a_missing_state_is_refused(self) -> None:
        with self.assertRaisesRegex(MbError, "state directory"):
            self.job()  # `ci subject` has not run.
        self.begin()
        source = self.mod / "scripts/ci/mod_base_build_adapter.py"
        source.write_bytes(source.read_bytes() + b"# changed after the config was written\n")
        with self.assertRaisesRegex(MbError, "differs from its configured hash"):
            self.job()


class WorkerRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        identity.create_state(self.state)

    @staticmethod
    def document() -> dict:
        return {"boundary": {"home": "/home/runner", "uid": 1001, "gid": 1001, "device": 2049, "inode": 4242,
                             "original_mode": 0o750},
                "accounts": {"candidate": {"uid": 2000, "gid": 2000}, "validator": {"uid": 2001, "gid": 2001}},
                "python": "/opt/hostedtoolcache/Python/3.12.7/x64/bin/python3",
                "java_homes": ["/opt/hostedtoolcache/jdk/17", "/opt/hostedtoolcache/jdk/21"],
                "tools": {"roots": ["/opt/hostedtoolcache/Python/3.12.7/x64", "/opt/hostedtoolcache/jdk/17",
                                    "/opt/hostedtoolcache/jdk/21"],
                          "metadata_sha256": "a" * 64, "files": 12, "entries": 20, "total_bytes": 4096},
                "config_sha256": "b" * 64}

    def write(self, raw: bytes, *, mode: int = 0o600) -> None:
        path = self.state / lifecycle.WORKER_NAME
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_bytes(raw)
        path.chmod(mode)

    def test_the_record_gives_later_steps_the_boundary_accounts_tools_and_config_digest(self) -> None:
        document = self.document()
        identity.write_state_record(self.state, lifecycle.WORKER_NAME, canonical_json(document))
        worker = lifecycle.read_worker(self.state)
        self.assertEqual(worker.boundary, HostBoundary("/home/runner", 1001, 1001, 2049, 4242, 0o750))
        self.assertEqual(dict(worker.accounts), {
            "candidate": WorkerAccount("candidate", 2000, 2000, "/tmp/mod-base-sandbox-boundary/mod-base-worker/"
                                                               "candidate-home"),
            "validator": WorkerAccount("validator", 2001, 2001, "/tmp/mod-base-sandbox-boundary/mod-base-worker/"
                                                               "validator-home")})
        self.assertEqual(worker.validator, worker.accounts["validator"])
        self.assertEqual((worker.python, worker.java_homes, worker.java_home),
                         (document["python"], tuple(document["java_homes"]), "/opt/hostedtoolcache/jdk/17"))
        self.assertEqual(asdict(worker.tools), {**document["tools"], "roots": tuple(document["tools"]["roots"])})
        self.assertEqual(worker.config_sha256, "b" * 64)
        self.assertEqual(lifecycle._worker_document(worker), document)

    def test_a_job_with_the_validator_alone_and_no_jdk_is_a_complete_record(self) -> None:
        document = self.document()
        document["accounts"]["candidate"] = None
        document["java_homes"] = []
        self.write(canonical_json(document))
        worker = lifecycle.read_worker(self.state)
        self.assertEqual((set(worker.accounts), worker.java_homes, worker.java_home), ({"validator"}, (), None))
        self.assertEqual(lifecycle._worker_document(worker), document)

    def test_any_other_shape_value_or_encoding_is_refused(self) -> None:
        def changed(mutate) -> bytes:
            document = self.document()
            mutate(document)
            return canonical_json(document)

        cases = {
            "unknown key": changed(lambda d: d.update(command=["sh"])),
            "missing key": changed(lambda d: d.pop("config_sha256")),
            "missing validator": changed(lambda d: d["accounts"].update(validator=None)),
            "missing candidate key": changed(lambda d: d["accounts"].pop("candidate")),
            "a third role": changed(lambda d: d["accounts"].update(worker={"uid": 2002, "gid": 2002})),
            "accounts share a user": changed(lambda d: d["accounts"]["candidate"].update(uid=2001)),
            "accounts share a group": changed(lambda d: d["accounts"]["candidate"].update(gid=2001)),
            "account is the runner": changed(lambda d: d["accounts"]["validator"].update(uid=1001)),
            "account in the runner's group": changed(lambda d: d["accounts"]["validator"].update(gid=1001)),
            "system account": changed(lambda d: d["accounts"]["validator"].update(uid=limits.MIN_CI_WORKER_UID - 1)),
            "boolean id": changed(lambda d: d["accounts"]["validator"].update(uid=True)),
            "another home": changed(lambda d: d["boundary"].update(home="/home/other")),
            "root runner": changed(lambda d: d["boundary"].update(uid=0)),
            "mode out of range": changed(lambda d: d["boundary"].update(original_mode=0o10000)),
            "relative interpreter": changed(lambda d: d.update(python="bin/python3")),
            "traversing interpreter": changed(lambda d: d.update(python="/opt/../bin/python3")),
            "repeated JDK": changed(lambda d: d.update(java_homes=["/opt/jdk", "/opt/jdk"])),
            "JDK is no path": changed(lambda d: d.update(java_homes=[17])),
            "no tool root": changed(lambda d: d["tools"].update(roots=[])),
            "too many tool roots": changed(lambda d: d["tools"].update(
                roots=[f"/opt/tool-{index}" for index in range(limits.MAX_CI_TOOL_ROOTS + 1)])),
            "tool digest": changed(lambda d: d["tools"].update(metadata_sha256="A" * 64)),
            "tool count": changed(lambda d: d["tools"].update(entries=0)),
            "config digest": changed(lambda d: d.update(config_sha256="b" * 63)),
            "not canonical": json.dumps(self.document(), indent=2).encode("utf-8"),
            "duplicate key": canonical_json(self.document()).replace(b'"python":', b'"python":"/x/bin/y","python":', 1),
            "not JSON": b"\xff",
            "a list": b"[]\n",
        }
        for label, raw in cases.items():
            self.write(raw)
            with self.subTest(label=label), self.assertRaises(MbError):
                lifecycle.read_worker(self.state)
        self.write(canonical_json(self.document()))
        lifecycle.read_worker(self.state)

    def test_only_a_private_single_bounded_file_of_this_user_is_read(self) -> None:
        raw = canonical_json(self.document())
        path = self.state / lifecycle.WORKER_NAME
        for mode in (0o644, 0o640, 0o400, 0o700):
            self.write(raw, mode=mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(MbError):
                lifecycle.read_worker(self.state)
        self.write(raw)
        os.link(path, self.state / "alias.json")
        with self.assertRaises(MbError):
            lifecycle.read_worker(self.state)
        (self.state / "alias.json").unlink()
        lifecycle.read_worker(self.state)
        other = self.state.parent / "elsewhere.json"
        other.write_bytes(raw)
        other.chmod(0o600)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(MbError):
            lifecycle.read_worker(self.state)
        path.unlink()
        with self.assertRaises(MbError):
            lifecycle.read_worker(self.state)  # Missing.
        self.write(b"")
        with self.assertRaises(MbError):
            lifecycle.read_worker(self.state)
        with mock.patch.object(limits, "MAX_CI_WORKER_RECORD_BYTES", len(raw) - 1):
            self.write(raw)
            with self.assertRaises(MbError):
                lifecycle.read_worker(self.state)
        self.state.chmod(0o755)
        with self.assertRaises(MbError):
            lifecycle.read_worker(self.state)
        self.state.chmod(0o700)
        lifecycle.read_worker(self.state)


class ToolRootTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        (self.base / "cpython/bin").mkdir(parents=True)
        (self.base / "cpython/bin/python3.12").write_bytes(b"elf")
        (self.base / "cpython/bin/python3").symlink_to("python3.12")
        (self.base / "venv/bin").mkdir(parents=True)
        (self.base / "venv/bin/python3").symlink_to(self.base / "cpython/bin/python3")

    def test_roots_are_the_named_prefix_the_resolved_prefix_and_every_jdk(self) -> None:
        base = str(self.base)
        self.assertEqual(lifecycle.tool_roots(f"{base}/cpython/bin/python3", ()), (f"{base}/cpython",))
        self.assertEqual(lifecycle.tool_roots(f"{base}/venv/bin/python3", ("/opt/jdk-17", "/opt/jdk-21")),
                         (f"{base}/venv", f"{base}/cpython", "/opt/jdk-17", "/opt/jdk-21"))
        self.assertEqual(lifecycle.tool_roots(f"{base}/cpython/bin/python3", (f"{base}/cpython", "/opt/jdk-17")),
                         (f"{base}/cpython", "/opt/jdk-17"))

    def test_an_interpreter_outside_a_prefix_of_its_own_and_unclear_paths_are_refused(self) -> None:
        base = str(self.base)
        (self.base / "odd").mkdir()
        (self.base / "odd/python3.12").write_bytes(b"elf")
        (self.base / "venv/bin/stray").symlink_to(self.base / "odd/python3.12")
        for python in ("python3", "bin/python3", f"{base}/cpython/bin/../bin/python3", f"{base}/cpython//bin/python3",
                       f"{base}/odd/python3.12", f"{base}/venv/bin/stray", "/bin/python3", "/python3",
                       f"{base}/cpython/bin/python3\n", "", None, 3):
            with self.subTest(python=python), self.assertRaises(MbError):
                lifecycle.tool_roots(python, ())
        for homes in (("/opt/jdk", "/opt/jdk"), ("jdk",), ("/opt/jdk/../other",), ("/opt/jdk:/other",), ("",)):
            with self.subTest(homes=homes), self.assertRaises(MbError):
                lifecycle.tool_roots(f"{base}/cpython/bin/python3", homes)


class ArgumentTests(JobCase):
    """Usage and admission of the three verbs; none of these cases may reach the host."""

    def assert_untouched(self) -> None:
        self.assertEqual(self.records(), {"identity.json"})
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))

    def test_malformed_flags_are_usage_errors_before_anything_is_read(self) -> None:
        self.begin()
        python = ("--python", "/opt/python/bin/python3")
        cases = [("worker-prepare",), ("worker-prepare", "--roles", "validator"), ("worker-prepare", *python),
                 ("worker-prepare", "--roles", "candidate", *python),
                 ("worker-prepare", "--roles", "validator+candidate", *python),
                 ("worker-prepare", "--roles", "validator", "--python", "python3"),
                 ("worker-prepare", "--roles", "validator", "--python", "/opt/../bin/python3"),
                 ("worker-prepare", "--roles", "validator", "--python", ""),
                 ("worker-prepare", "--roles", "validator", *python, "--java-home", "jdk"),
                 ("worker-prepare", "--roles", "validator", *python, "--java-home", ""),
                 ("worker-prepare", "--roles", "validator", *python, "--candidate", "candidate"),
                 ("plan", "--expect-sha256", "0" * 63), ("plan", "--expect-sha256", "A" * 64),
                 ("plan", "--expect-sha256", ""), ("plan", "--candidate", ""), ("plan", "--roles", "validator"),
                 ("worker-finish", "--roles", "validator"), ("worker-finish", "extra")]
        for arguments in cases:
            code, stdout, stderr = self.commands.run(*arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assert_untouched()

    def test_worker_prepare_admits_the_runner_layout_before_it_records_or_changes_anything(self) -> None:
        self.begin()
        arguments = ("worker-prepare", "--roles", "candidate+validator", "--python", "/opt/python/bin/python3")
        refusals = [({}, "environment: RUNNER_ENVIRONMENT is required for this command"),
                    ({"RUNNER_ENVIRONMENT": "github-hosted"},
                     "environment: GITHUB_WORKSPACE is required for this command"),
                    ({**HOSTED, "RUNNER_TEMP": ""}, "environment: RUNNER_TEMP is required for this command"),
                    ({**HOSTED, "RUNNER_ENVIRONMENT": "self-hosted"},
                     "ci-worker: host fence requires the initial GitHub-hosted Linux layout"),
                    ({**HOSTED, "GITHUB_WORKSPACE": "/tmp/work"},
                     "ci-worker: host workspace and temp must be below the runner home"),
                    ({**HOSTED, "RUNNER_TEMP": HOSTED["GITHUB_WORKSPACE"]},
                     "ci-worker: host workspace and temp must be distinct directories")]
        for environment, message in refusals:
            self.commands.environment = {**self.environment, **environment}
            with self.subTest(environment=environment):
                self.assertEqual(self.commands.run(*arguments), (2, "", f"mod_base: {message}\n"))
        # An interpreter that is not <prefix>/bin/<name> and a repeated JDK stop even earlier.
        self.commands.environment = {**self.environment, **HOSTED, "RUNNER_ENVIRONMENT": "self-hosted"}
        for extra in (("--python", "/opt/python3"), ("--java-home", "/opt/jdk", "--java-home", "/opt/jdk")):
            code, stdout, stderr = self.commands.run(*arguments, *extra)
            with self.subTest(extra=extra):
                self.assertEqual((code, stdout), (2, ""))
                self.assertNotIn("host fence", stderr)
        self.assert_untouched()

    def test_every_verb_needs_the_state_of_its_job(self) -> None:
        missing = ("worker-prepare", "--roles", "validator", "--python", "/opt/python/bin/python3")
        for arguments in (missing, ("plan",)):
            code, stdout, stderr = self.commands.run(*arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("ci-state: cannot open the state directory", stderr)
        self.assertFalse(self.state.exists())
        self.begin()
        code, stdout, stderr = self.commands.run("plan")  # No worker was prepared.
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("ci-state: cannot read the state record worker.json", stderr)
        self.assert_untouched()

    def test_a_command_of_another_run_of_the_repository_cannot_use_this_state(self) -> None:
        self.begin()
        self.commands.environment = {**self.environment, "GITHUB_SHA": "e" * 40}
        for arguments in (("worker-prepare", "--roles", "validator", "--python", "/opt/python/bin/python3"), ("plan",)):
            code, stdout, stderr = self.commands.run(*arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("the state directory belongs to another repository or controller commit", stderr)
        self.assert_untouched()


class CandidateFileTests(JobCase):
    def expected(self) -> dict[str, bytes]:
        return candidate_sources(self.mod)

    # -- from the API -----------------------------------------------------------------------------------

    def test_the_api_serves_every_candidate_file_of_the_tested_tree_in_one_request_each(self) -> None:
        fixture.seed_tested_tree(self.api, self.mod)
        job = self.begin()
        sources = lifecycle.api_candidate_files(self.api, job)
        self.assertEqual(sources, self.expected())
        self.assertEqual(list(sources), SOURCE_NAMES)  # In the order they are staged and requested.
        self.assertEqual(sources["gradle-properties"], (self.mod / "gradle.properties").read_bytes())
        # Rule 4: the tree and one blob per file. The command's client is built with MAX_CI_PLAN_REQUESTS,
        # which also covers a config with every extra plan input it may name.
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS + PLAN_REQUESTS)
        self.assertLessEqual(1 + 2 + limits.MAX_CI_PLAN_INPUTS, limits.MAX_CI_PLAN_REQUESTS)
        self.assertLessEqual(limits.MAX_CI_PLAN_REQUESTS, 60)
        self.assertEqual(self.api.mutations, [])

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        self.api, self.pull = h.github(max_requests=SUBJECT_REQUESTS + 2)
        self.commands.api = self.api
        fixture.seed_tested_tree(self.api, self.mod)
        job = self.begin()
        with self.assertRaises(MbError) as caught:
            lifecycle.api_candidate_files(self.api, job)
        self.assertEqual(caught.exception.reason, "request-budget")
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS + 2)

    def test_a_link_a_submodule_a_missing_empty_or_oversized_file_is_no_candidate_file(self) -> None:
        job = self.begin()
        inventory = (self.mod / "release/inventory.json").read_bytes()
        blob = self.api.add_blob(inventory)
        row = {"path": "release/inventory.json", "mode": "100644", "type": "blob", "sha": blob, "size": len(inventory)}
        empty = self.api.add_blob(b"")
        replacements = {
            "a symbolic link": {"release/inventory.json": {**row, "mode": "120000"}},
            "a submodule": {"release/inventory.json": {**row, "mode": "160000", "type": "commit"}},
            "a directory": {"release/inventory.json": {"path": "release/inventory.json", "mode": "040000",
                                                       "type": "tree", "sha": "9" * 40}},
            "an empty file": {"release/inventory.json": {**row, "sha": empty, "size": 0}},
            "an oversized file": {"release/inventory.json": {**row, "size": limits.MAX_CI_PLAN_SOURCE_BYTES + 1}},
            "no size": {"release/inventory.json": {key: value for key, value in row.items() if key != "size"}},
            "another size than its blob": {"release/inventory.json": {**row, "size": len(inventory) + 1}},
            "a parent that is a file": {"release": {"path": "release", "mode": "100644", "type": "blob",
                                                    "sha": blob, "size": len(inventory)}},
            "a parent that is a link": {"e2e": {"path": "e2e", "mode": "120000", "type": "blob", "sha": blob,
                                                "size": len(inventory)}},
            "an extra plan input that is a link": {"gradle.properties": {"path": "gradle.properties", "mode": "120000",
                                                                         "type": "blob", "sha": blob,
                                                                         "size": len(inventory)}},
        }
        for index, (label, replace_rows) in enumerate(replacements.items()):
            tree = f"{index + 1:040x}"
            fixture.seed_tested_tree(self.api, self.mod, tree=tree, replace=replace_rows)
            other = lifecycle.Job(**{**asdict_shallow(job), "record": with_tree(job.record, tree)})
            with self.subTest(label=label), self.assertRaises(MbError):
                lifecycle.api_candidate_files(self.api, other)
        absent = "a" * 39 + "b"
        rows = [{"path": "release", "mode": "040000", "type": "tree", "sha": "9" * 40}]
        self.api.add_tree(absent, rows)
        with self.assertRaises(MbError):
            lifecycle.api_candidate_files(self.api, lifecycle.Job(**{**asdict_shallow(job),
                                                                     "record": with_tree(job.record, absent)}))
        truncated = "a" * 39 + "c"
        self.api.add_tree(truncated, rows, truncated=True)
        with self.assertRaises(MbError):
            lifecycle.api_candidate_files(self.api, lifecycle.Job(**{**asdict_shallow(job),
                                                                     "record": with_tree(job.record, truncated)}))

    def test_a_client_of_another_repository_is_refused_before_any_request(self) -> None:
        job = self.begin()
        other = FakeGitHub(repository="example/other", default_branch="main")
        with self.assertRaises(MbError):
            lifecycle.api_candidate_files(other, job)
        self.assertEqual(other.request_count, 0)

    # -- from the candidate checkout ----------------------------------------------------------------------

    def checkout_job(self, mod: Path | None = None, *, tree: str | None = None) -> lifecycle.Job:
        if not Path(fixture.GIT).exists():
            self.skipTest("git is not installed at /usr/bin/git")
        self.checkout = self.temporary / "candidate"
        self.commit, real_tree = fixture.commit_candidate(mod or self.mod, self.checkout)
        fixture.retarget(self.api, self.pull, self.commit, tree or real_tree)
        return self.begin()

    def test_the_checkout_serves_both_files_from_its_objects_without_the_api(self) -> None:
        job = self.checkout_job()
        self.assertEqual(job.subject["tested_sha"], self.commit)
        # The working files are not the tested commit: change one, remove the other.
        (self.checkout / "release/inventory.json").write_bytes(b'{"faults": "working file"}\n')
        (self.checkout / "e2e/scenario-contract.json").unlink()
        self.assertEqual(lifecycle.checkout_candidate_files(self.checkout, job), self.expected())
        relative = os.path.relpath(self.checkout)
        self.assertEqual(lifecycle.checkout_candidate_files(Path(relative), job), self.expected())
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS)

    def test_a_replacement_object_never_stands_in_for_a_candidate_blob(self) -> None:
        job = self.checkout_job()
        original = fixture.git(self.checkout, "rev-parse", f"{self.commit}:release/inventory.json")
        (self.temporary / "forged.json").write_bytes(b'{"forged": true}\n')
        forged = fixture.git(self.checkout, "hash-object", "-w", str(self.temporary / "forged.json"))
        fixture.git(self.checkout, "replace", original, forged)
        self.assertEqual(fixture.git(self.checkout, "cat-file", "blob", original), '{"forged": true}')
        self.assertEqual(lifecycle.checkout_candidate_files(self.checkout, job), self.expected())

    def test_a_checkout_at_another_commit_or_with_another_tree_is_refused(self) -> None:
        job = self.checkout_job()
        (self.checkout / "release/inventory.json").write_bytes(b"{}\n")
        fixture.git(self.checkout, "commit", "-q", "-a", "-m", "moved on")
        with self.assertRaisesRegex(MbError, "not at the tested commit"):
            lifecycle.checkout_candidate_files(self.checkout, job)
        fixture.git(self.checkout, "checkout", "-q", "--detach", self.commit)
        self.assertEqual(lifecycle.checkout_candidate_files(self.checkout, job), self.expected())
        other = lifecycle.Job(**{**asdict_shallow(job), "record": with_tree(job.record, "f" * 40)})
        with self.assertRaisesRegex(MbError, "does not have the tested tree"):
            lifecycle.checkout_candidate_files(self.checkout, other)

    def test_a_directory_without_its_own_git_directory_is_no_checkout(self) -> None:
        job = self.checkout_job()
        plain = self.temporary / "plain"
        plain.mkdir()
        nested = self.checkout / "nested"  # Inside a repository, but not one itself: nothing is discovered.
        nested.mkdir()
        pointer = self.temporary / "pointer"
        pointer.mkdir()
        (pointer / ".git").write_text(f"gitdir: {self.checkout / '.git'}\n", encoding="utf-8")
        linked = self.temporary / "linked"
        linked.mkdir()
        (linked / ".git").symlink_to(self.checkout / ".git")
        for directory in (plain, nested, pointer, linked, self.temporary / "absent"):
            with self.subTest(directory=directory.name), self.assertRaisesRegex(MbError, "no Git directory of its own"):
                lifecycle.checkout_candidate_files(directory, job)

    def refused_candidate_tree(self, *, link: bool) -> None:
        # The protected config comes from the protected mod; only the candidate tree differs.
        mod = h.materialize(self.temporary / "candidate-mod")
        (mod / "release/inventory.json").unlink()
        if link:
            (mod / "release/inventory.json").symlink_to("../e2e/scenario-contract.json")
        job = self.checkout_job(mod)
        with self.assertRaisesRegex(MbError, "not a regular file"):
            lifecycle.checkout_candidate_files(self.checkout, job)

    def test_a_candidate_path_that_is_a_link_in_the_commit_is_refused(self) -> None:
        self.refused_candidate_tree(link=True)
        self.assertTrue((self.checkout / "release/inventory.json").is_symlink())

    def test_a_candidate_path_that_is_missing_in_the_commit_is_refused(self) -> None:
        self.refused_candidate_tree(link=False)


def asdict_shallow(job: lifecycle.Job) -> dict:
    return {name: getattr(job, name) for name in job.__dataclass_fields__}


def with_tree(record: dict, tree: str) -> dict:
    changed = copy.deepcopy(record)
    changed["subject"]["tested_tree"] = tree
    return changed


class InputRootTests(unittest.TestCase):
    """``validation-input/`` as real directories: what the validator may read, exactly."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / "validation-input"
        self.plan, self.sources = ci_plan_inputs()
        self.digests = inputs.plan_source_digests(self.plan)

    def stage(self, *, plan: bool) -> None:
        inputs.materialize_validation_inputs(self.root, sources=self.sources, plan=self.plan if plan else None)

    def contents(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.root.iterdir()}

    def test_before_the_plan_the_root_is_the_candidate_files(self) -> None:
        self.stage(plan=False)
        self.assertEqual(self.contents(), self.sources)
        self.assertEqual(set(self.sources), set(SOURCE_NAMES))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        inputs.verify_validation_inputs(self.root, digests=self.digests)
        with self.assertRaises(MbError):  # A hook that runs against the plan needs the plan there.
            inputs.verify_validation_plan(self.root, plan=self.plan)
        with self.assertRaises(MbError):
            inputs.verify_validation_inputs(self.root, digests=self.digests, plan=self.plan)
        fewer = {name: digest for name, digest in self.digests.items() if name != "gradle-properties"}
        with self.assertRaises(MbError):  # An extra plan input nobody named is an undeclared file.
            inputs.verify_validation_inputs(self.root, digests=fewer)
        with self.assertRaises(MbError):
            inputs.verify_validation_inputs(self.root, digests={**self.digests, "another-input": "0" * 64})
        with self.assertRaises(MbError):  # An existing root is never replaced or completed in place.
            self.stage(plan=True)
        self.assertEqual(set(self.contents()), set(SOURCE_NAMES))

    def test_with_the_plan_the_root_is_the_plan_and_the_files_its_identity_binds(self) -> None:
        self.stage(plan=True)
        self.assertEqual(self.contents(), {"ci-plan.json": canonical_json(self.plan), **self.sources})
        self.assertEqual(inputs.verify_validation_plan(self.root, plan=self.plan), self.plan)
        with self.assertRaises(MbError):  # `derive_plan` must not find a plan.
            inputs.verify_validation_inputs(self.root, digests=self.digests)
        for change in (lambda plan: plan["identity"].update(inventory_sha256="0" * 64),
                       lambda plan: plan["identity"].update(scenario_sha256="0" * 64),
                       lambda plan: plan["plan_inputs"][0].update(sha256="0" * 64),
                       lambda plan: plan.update(plan_inputs=[])):
            other = copy.deepcopy(self.plan)
            change(other)
            other["plan_sha256"] = planning.plan_sha256(other)
            with self.assertRaises(MbError):  # Another plan than the one these files were bound by.
                inputs.verify_validation_plan(self.root, plan=other)
        self.assertEqual(len(list(self.base.iterdir())), 1)  # No stage is left beside the root.

    def test_changed_extra_missing_linked_and_special_entries_are_refused(self) -> None:
        for plan in (False, True):
            def verify() -> None:
                inputs.verify_validation_inputs(self.root, digests=self.digests, plan=self.plan if plan else None)

            self.stage(plan=plan)
            verify()
            for name in self.contents():
                original = (self.root / name).read_bytes()
                (self.root / name).write_bytes(original + b" ")
                with self.subTest(plan=plan, changed=name), self.assertRaises(MbError):
                    verify()
                (self.root / name).unlink()
                with self.subTest(plan=plan, missing=name), self.assertRaises(MbError):
                    verify()
                (self.root / name).symlink_to(self.base / "elsewhere")
                with self.subTest(plan=plan, link=name), self.assertRaises(MbError):
                    verify()
                (self.root / name).unlink()
                (self.root / name).write_bytes(original)
                verify()
            for kind in ("file", "directory", "symlink", "hardlink", "fifo"):
                extra = self.root / "extra.pth"
                if kind == "file":
                    extra.write_bytes(b"import os\n")
                elif kind == "directory":
                    extra.mkdir()
                elif kind == "symlink":
                    extra.symlink_to("/etc/passwd")
                elif kind == "hardlink":
                    os.link(self.root / "inventory", extra)
                else:
                    os.mkfifo(extra)
                with self.subTest(plan=plan, extra=kind), self.assertRaises(MbError):
                    verify()
                extra.rmdir() if kind == "directory" else extra.unlink()
            verify()
            for path in self.root.iterdir():
                path.unlink()
            self.root.rmdir()

    def test_discard_removes_exactly_the_expected_flat_directory(self) -> None:
        from pathlib import PurePosixPath

        self.stage(plan=False)
        names = tuple(self.sources)
        (self.root / "extra").write_bytes(b"x")
        with self.assertRaises(MbError):
            inputs._discard(PurePosixPath(str(self.root)), names)
        self.assertEqual(set(self.contents()), {*names, "extra"})  # Nothing was removed.
        (self.root / "extra").unlink()
        with self.assertRaises(MbError):
            inputs._discard(PurePosixPath(str(self.root)), ("inventory",))
        self.assertEqual(set(self.contents()), set(names))
        link = self.base / "link"
        link.symlink_to(self.root)
        with self.assertRaises(MbError):
            inputs._discard(PurePosixPath(str(link)), names)
        self.assertEqual(set(self.contents()), set(names))
        inputs._discard(PurePosixPath(str(self.root)), names)
        self.assertFalse(self.root.exists())
        self.assertTrue(link.is_symlink())


class PlanRecordTests(JobCase):
    def pure_plan(self, job: lifecycle.Job) -> dict:
        sandbox = h.Sandbox(self.temporary / "pure", protected=self.mod)
        sandbox.subject = job.subject
        return sandbox.derive_plan()

    def test_the_recorded_plan_is_read_back_canonical_and_of_this_subject(self) -> None:
        job = self.begin()
        plan = self.pure_plan(job)
        self.assertEqual(subject_of(plan["identity"]), job.subject)
        identity.write_state_record(self.state, lifecycle.PLAN_NAME, canonical_json(plan))
        self.assertEqual(lifecycle.PLAN_NAME, grammar.CI_PLAN_NAME)
        self.assertEqual(lifecycle.read_plan(job), plan)
        # A plan is derived once: nothing of the worker is touched when the record exists.
        with self.assertRaisesRegex(MbError, "already has a plan"):
            lifecycle.derive_plan(job, None, None, expected_sha256=None, log=self.fail)

    def test_a_plan_of_another_subject_or_in_another_encoding_is_refused(self) -> None:
        job = self.begin()
        plan = self.pure_plan(job)
        other = copy.deepcopy(plan)
        other["identity"]["head_branch"] = "feature/other"
        other["plan_sha256"] = planning.plan_sha256(other)
        unbound = {**plan, "plan_sha256": "0" * 64}
        path = self.state / lifecycle.PLAN_NAME
        for label, raw in (("another subject", canonical_json(other)), ("hash does not bind", canonical_json(unbound)),
                           ("not canonical", json.dumps(plan).encode("utf-8")), ("truncated", canonical_json(plan)[:-9])):
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.subTest(label=label), self.assertRaises(MbError):
                lifecycle.read_plan(job)
            path.unlink()
        with self.assertRaises(MbError):
            lifecycle.read_plan(job)


class FinishTests(JobCase):
    """``finish_worker`` where no account exists. The passwd lookup is pinned to "absent", so
    that these cases never touch an account of a hosted run on the same machine."""

    def finish(self, state: Path) -> dict:
        with mock.patch.object(lifecycle, "worker_account_exists", return_value=False), \
                mock.patch.object(lifecycle, "terminate_worker") as terminate, \
                mock.patch.object(lifecycle, "lock_worker_account") as lock:
            try:
                return lifecycle.finish_worker(state)
            finally:
                terminate.assert_not_called()
                lock.assert_not_called()

    def test_a_job_that_prepared_nothing_reports_absent_accounts_and_an_untouched_home(self) -> None:
        nothing = {"accounts": {"candidate": "absent", "validator": "absent"}, "home_mode": None}
        self.assertEqual(self.finish(self.state), nothing)  # Not even `ci subject` ran.
        self.assertFalse(self.state.exists())
        self.begin()
        self.assertEqual(self.finish(self.state), nothing)
        self.assertEqual(self.finish(self.state), nothing)
        self.assertEqual(self.records(), {"identity.json"})

    def test_a_host_record_that_cannot_be_trusted_never_reaches_a_chmod(self) -> None:
        self.begin()
        path = self.state / lifecycle.HOST_NAME
        boundary = {"home": "/home/runner", "uid": os.getuid(), "gid": os.getgid(), "device": 1, "inode": 1,
                    "original_mode": 0o750}
        cases = {"not canonical": json.dumps({"boundary": boundary}).encode("utf-8"),
                 "unknown key": canonical_json({"boundary": boundary, "roles": ["validator"]}),
                 "another home": canonical_json({"boundary": {**boundary, "home": "/root"}}),
                 "mode out of range": canonical_json({"boundary": {**boundary, "original_mode": 0o17777}}),
                 "another runner": canonical_json({"boundary": {**boundary, "uid": os.getuid() + 1}}),
                 # Well formed, but not the directory /home/runner is (or there is none on this machine).
                 "another directory": canonical_json({"boundary": boundary})}
        for label, raw in cases.items():
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.subTest(label=label), mock.patch.object(os, "fchmod") as chmod, self.assertRaises(MbError):
                self.finish(self.state)
            chmod.assert_not_called()
            path.unlink()
        path.write_bytes(canonical_json({"boundary": boundary}))  # Readable by others: not a state record.
        path.chmod(0o644)
        with mock.patch.object(os, "fchmod") as chmod, self.assertRaises(MbError):
            self.finish(self.state)
        chmod.assert_not_called()

    def test_accounts_without_a_record_of_their_preparation_are_locked_and_reported(self) -> None:
        self.begin()
        account = WorkerAccount("validator", 2001, 2001, "/tmp/mod-base-sandbox-boundary/mod-base-worker/validator-home")
        with mock.patch.object(lifecycle, "worker_account_exists", side_effect=lambda role: role == "validator"), \
                mock.patch.object(lifecycle, "authenticate_worker_account", return_value=account), \
                mock.patch.object(lifecycle, "terminate_worker") as terminate, \
                mock.patch.object(lifecycle, "worker_processes", return_value=False), \
                self.assertRaisesRegex(MbError, "no record of preparing"):
            lifecycle.finish_worker(self.state)
        terminate.assert_called_once_with(account)

    def test_an_account_that_cannot_be_stopped_keeps_the_home_closed(self) -> None:
        self.begin()
        candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/mod-base-sandbox-boundary/mod-base-worker/candidate-home")
        validator = WorkerAccount("validator", 2001, 2001, "/tmp/mod-base-sandbox-boundary/mod-base-worker/validator-home")
        accounts = {"candidate": candidate, "validator": validator}
        boundary = {"home": "/home/runner", "uid": os.getuid(), "gid": os.getgid(), "device": 1, "inode": 1,
                    "original_mode": 0o750}
        identity.write_state_record(self.state, lifecycle.HOST_NAME, canonical_json({"boundary": boundary}))

        def finish(*, terminate=None, authenticate=None, alive=()):
            with mock.patch.object(lifecycle, "worker_account_exists", return_value=True), \
                    mock.patch.object(lifecycle, "authenticate_worker_account",
                                      side_effect=authenticate or (lambda role: accounts[role])), \
                    mock.patch.object(lifecycle, "terminate_worker", side_effect=terminate) as stop, \
                    mock.patch.object(lifecycle, "lock_worker_account") as lock, \
                    mock.patch.object(lifecycle, "worker_processes", side_effect=lambda account: account in alive), \
                    mock.patch.object(lifecycle, "restore_worker_host") as restore:
                try:
                    return lifecycle.finish_worker(self.state)
                finally:
                    self.stopped = [call.args[0] for call in stop.call_args_list]
                    self.locked = [call.args[0] for call in lock.call_args_list]
                    self.restored = restore.call_args_list

        self.assertEqual(finish(), {"accounts": {"candidate": "locked", "validator": "locked"}, "home_mode": 0o750})
        self.assertEqual((self.stopped, self.locked), ([candidate, validator], []))
        self.assertEqual(self.restored, [mock.call(HostBoundary(**boundary))])

        def survivor(account):
            if account == candidate:
                raise WorkerError("worker processes survived termination")

        with self.assertRaisesRegex(MbError, "survived"):
            finish(terminate=survivor)
        self.assertEqual((self.stopped, self.restored), ([candidate, validator], []))  # The other is still swept.

        def foreign(role):
            if role == "candidate":
                raise WorkerError("cannot authenticate disposable account")
            return accounts[role]

        with self.assertRaisesRegex(MbError, "cannot authenticate"):
            finish(authenticate=foreign)
        # Locked by name, never signalled; the validator is swept; the home stays closed.
        self.assertEqual((self.stopped, self.locked, self.restored), ([validator], ["candidate"], []))
        with self.assertRaisesRegex(MbError, "still owns a process"):
            finish(alive=(validator,))
        self.assertEqual(self.restored, [])


class RestingTests(unittest.TestCase):
    candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/candidate-home")
    validator = WorkerAccount("validator", 2001, 2001, "/tmp/validator-home")

    def rest(self, block=None, *, failing=()):
        stopped = []

        def terminate(account):
            stopped.append(account.role)
            if account.role in failing:
                raise WorkerError(f"{account.role} survived")

        accounts = {}
        with mock.patch.object(lifecycle, "terminate_worker", side_effect=terminate):
            try:
                with lifecycle.resting(accounts):
                    accounts["candidate"] = self.candidate  # Allocated inside the block.
                    accounts["validator"] = self.validator
                    if block is not None:
                        raise block
            finally:
                self.stopped = stopped

    def test_every_account_is_stopped_after_the_block_whatever_happened(self) -> None:
        self.rest()
        self.assertEqual(self.stopped, ["candidate", "validator"])
        with self.assertRaisesRegex(MbError, "hook failed"):
            self.rest(MbError("hook failed"))
        self.assertEqual(self.stopped, ["candidate", "validator"])
        with self.assertRaises(KeyboardInterrupt):
            self.rest(KeyboardInterrupt())
        self.assertEqual(self.stopped, ["candidate", "validator"])

    def test_a_failure_to_stop_one_account_still_stops_the_other_and_is_reported(self) -> None:
        with self.assertRaisesRegex(MbError, "candidate survived"):
            self.rest(failing=("candidate",))
        self.assertEqual(self.stopped, ["candidate", "validator"])
        with self.assertRaisesRegex(MbError, "candidate survived"):
            self.rest(failing=("candidate", "validator"))
        with self.assertRaisesRegex(MbError, "hook failed"):  # The block's own failure is the one reported.
            self.rest(MbError("hook failed"), failing=("candidate", "validator"))
        self.assertEqual(self.stopped, ["candidate", "validator"])


class PlanCommandTests(JobCase):
    """``ci plan`` up to the worker: where the candidate files come from and what it outputs.

    The worker and the hook run need accounts, so they are replaced by the pure derivation of the
    harness; the hosted class runs the same command without any replacement.
    """

    def run_plan(self, *arguments: str) -> tuple[int, str, str]:
        self.received = []
        self.runs = getattr(self, "runs", 0) + 1

        def derive(job, worker, files, *, expected_sha256, log, candidate_pin=None):
            self.assertIsNone(candidate_pin)
            self.received.append((worker, files, expected_sha256))
            sandbox = h.Sandbox(self.temporary / f"pure-{self.runs}", protected=self.mod)
            sandbox.subject = job.subject
            return planning.require_plan(sandbox.derive_plan(), subject=job.subject, expected_sha256=expected_sha256)

        with mock.patch.object(lifecycle, "open_worker", return_value="worker") as opened, \
                mock.patch.object(lifecycle, "derive_plan", side_effect=derive):
            result = self.commands.run("plan", "--github-output", str(self.output), *arguments)
        self.opened = opened.call_count
        return result

    def test_without_a_checkout_the_files_come_from_the_api_through_a_budgeted_client(self) -> None:
        fixture.seed_tested_tree(self.api, self.mod)
        self.begin()
        code, stdout, stderr = self.run_plan()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.received, [("worker", candidate_sources(self.mod), None)])
        self.assertEqual(list(self.received[0][1]), SOURCE_NAMES)
        self.assertEqual(self.commands.budgets, [limits.MAX_CI_SUBJECT_REQUESTS, limits.MAX_CI_PLAN_REQUESTS])
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS + PLAN_REQUESTS)
        lines = self.output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], [f"tested_sha={h.TESTED_SHA}", "pr_number=7"])
        self.assertRegex(lines[2], r"^plan_sha256=[0-9a-f]{64}$")
        self.assertEqual(lines[3:], ['candidate_kit_sha=', 'targets=["1.20.1","1.21.1"]',
                                     'lanes=["fabric-1.20.1","forge-1.20.1","fabric-1.21.1"]'])
        self.assertEqual(stdout, f"plan: {lines[2].split('=')[1]} with 2 targets and 3 lanes\n")

    def test_with_a_checkout_no_client_is_built_and_the_expected_hash_is_passed_on(self) -> None:
        if not Path(fixture.GIT).exists():
            self.skipTest("git is not installed at /usr/bin/git")
        checkout = self.temporary / "candidate"
        fixture.retarget(self.api, self.pull, *fixture.commit_candidate(self.mod, checkout))
        self.begin()
        code, _, stderr = self.run_plan("--candidate", str(checkout), "--expect-sha256", "0" * 64)
        self.assertEqual((code, stderr), (2, "mod_base: plan-mismatch: this job derived another plan than the one "
                                             "the generation agreed on\n"))
        self.assertEqual(self.received[0][2], "0" * 64)
        self.assertEqual(self.output.read_text(encoding="utf-8").count("\n"), 2)  # No plan output was written.
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))
        self.assertEqual(self.run_plan("--candidate", str(checkout))[0], 0)
        self.assertEqual(self.output.read_text(encoding="utf-8").count("\n"), 6)
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))

    def test_the_worker_is_opened_before_any_candidate_file_is_read(self) -> None:
        fixture.seed_tested_tree(self.api, self.mod)
        self.begin()
        with mock.patch.object(lifecycle, "open_worker", side_effect=MbError("no worker")), \
                mock.patch.object(lifecycle, "derive_plan") as derive:
            self.assertEqual(self.commands.run("plan"), (2, "", "mod_base: rejected: no worker\n"))
        derive.assert_not_called()
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))


if __name__ == "__main__":
    unittest.main()
