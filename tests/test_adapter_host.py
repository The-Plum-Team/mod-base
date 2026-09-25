"""The adapter host (SPEC §1.9): env scrub, argv allowlist, token gating, timeout, oversize and
schema rejection, plus the child's in-process dispatch and the ``ctx`` helpers."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from mod_base.adapter import api as adapter_api
from mod_base.adapter import host, host_child, protocol
from mod_base.adapter.protocol import HookFailed, HookUnsupported
from mod_base.errors import MbError
from mod_base.imaging.metrics import ImageError, SizePolicy
from mod_base.imaging.png import pattern_png
from mod_base.io.tree import TreeError
from mod_base.model import limits as lim
from mod_base.model.canonical import StrictJsonError
from mod_base.model.validators import DocumentError
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH
from tests.fixtures.mods import support

PROBE = '''
"""Probe adapter: behaviour chosen by probe_mode.json next to this file."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import probe_helper

ADAPTER_API = 1
_MODE = json.loads(Path(__file__).with_name("probe_mode.json").read_text())


def _report(ctx, extra=None):
    if "report" in _MODE:
        Path(_MODE["report"]).write_text(json.dumps({
            "environ": dict(os.environ), "argv": sys.argv[1:], "cwd": os.getcwd(), "api": ctx.api is not None,
            "api_writable": getattr(ctx.api, "writable", None), "api_budget": getattr(ctx.api, "_max_requests", None),
            "tmpdir": str(ctx.tmpdir),
            "helper": probe_helper.VALUE, "implementation_sha": ctx.implementation_sha, **(extra or {})}))


def _response_path():
    return sys.argv[sys.argv.index("--response") + 1]


def targets(ctx, branches):
    mode = _MODE["mode"]
    _report(ctx)
    if mode == "sleep":
        time.sleep(60)
    if mode == "grandchild":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        Path(_MODE["pid"]).write_text(str(child.pid))
    if mode == "oversize":
        with open(_response_path(), "wb") as stream:
            stream.write(b" " * (17 * 1024 * 1024))
        os._exit(0)
    if mode == "duplicate-key":
        with open(_response_path(), "w") as stream:
            stream.write('{"hook":"targets","hook":"targets"}')
        os._exit(0)
    if mode == "plant":
        os.symlink("/dev/null", _response_path())
    if mode == "crash":
        os._exit(5)
    if mode == "noisy-crash":
        print("first words\\nlast words", flush=True)
        os._exit(5)
    if mode == "chatty":
        chunk = b"x" * (1 << 20)
        for _ in range(64):
            os.write(1, chunk)
    if mode == "raise":
        raise ValueError("boom\\nsecond line\\x1b[31m red")
    if mode == "exit":
        sys.exit(3)
    target = {"key": "probe", "label": "Probe", "matrix_sha256": "0" * 64, "contract_sha256": "0" * 64,
              "subject": {"branch": "master", "commit": ctx.implementation_sha, "tree": "1" * 40}}
    if mode == "unknown-key":
        target["surprise"] = True
    return [target]


def authenticate_extensions(ctx, manifest, extensions):
    _report(ctx)
    return {"verified": sorted(extensions), "reuse_verified": False}


def verify_publication(ctx, promotion_draft):
    _report(ctx)
    return None
'''


def write_probe(root: Path) -> None:
    (root / "scripts/pages/mod_base_adapter.py").write_text(PROBE, encoding="utf-8")
    (root / "e2e/probe_helper.py").write_text("VALUE = 'imported through PYTHONPATH'\n", encoding="utf-8")
    (root / "scripts/pages/probe_mode.json").write_text('{"mode": "ok"}', encoding="utf-8")


def set_config(root: Path, **adapter: object) -> None:
    path = root / "site/mod-base.json"
    config = json.loads(path.read_text())
    config["adapter"].update(adapter)
    path.write_text(json.dumps(config, indent=2, sort_keys=True))


PROMOTION_DRAFT = {
    "kind": "mod-base.promotion", "schema_version": 1, "repository": "The-Plum-Team/qs-like",
    "implementation": {"branch": "master", "sha": "a" * 40, "run_id": 9000, "run_attempt": 1,
                       "workflow_ref": "The-Plum-Team/qs-like/.github/workflows/pages.yml@refs/heads/master"},
    "kit": {"repository": "The-Plum-Team/mod-base", "sha": support.KIT_SHA, "version": "0.9.0"},
    "heads": {"master": "a" * 40},
    "bundles": [{"key": "mc1.20.1", "collected_artifact_id": 1, "collected_digest": "sha256:" + "b" * 64,
                 "manifest_sha256": "c" * 64, "coverage_sha": "a" * 40, "selected_artifact_id": 2}],
    "families": [],
}


class HostTestCase(unittest.TestCase):
    """One probe repository per test (a copy of the qs-like fixture with the probe adapter)."""

    timeout_seconds = 120

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-host-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)

        def mutate(root: Path) -> None:
            write_probe(root)
            set_config(root, timeout_seconds=self.timeout_seconds)

        self.mod = support.materialize("qs_like", self.directory / "repo", mutate=mutate)
        self.report = self.directory / "report.json"

    def mode(self, mode: str, **extra: str) -> None:
        values = {"mode": mode, "report": str(self.report), **extra}
        (self.mod.root / "scripts/pages/probe_mode.json").write_text(json.dumps(values), encoding="utf-8")

    def invocation(self, *, job: str | None = None, token: str | None = None, workflow: str | None = None,
                   **environ: str):
        """A process of ``job``: a Pages callee job (``pages.yml``) for ``protocol.TOKEN_JOBS``,
        otherwise a mod-owned job of the source workflow (the prepare-evidence composite)."""

        if workflow is None:
            workflow = PAGES_WORKFLOW_PATH if job in protocol.TOKEN_JOBS else support.SOURCE_WORKFLOW
        values = support.environment(self.mod, job=job, token=token, workflow=workflow)
        values.update(environ)
        return support.invocation(self.mod, values)

    def reported(self) -> dict:
        return json.loads(self.report.read_text())


class ChildEnvironmentTest(HostTestCase):
    def test_child_runs_with_exactly_the_scrubbed_environment(self) -> None:
        self.mode("ok")
        invocation = self.invocation(job="collect", token="t0ken", GITHUB_TOKEN="other")
        with mock.patch.dict(os.environ, {"MB_TEST_SECRET": "leak", "GH_TOKEN": "leak", "AWS_SECRET": "leak",
                                          "PYTHONPATH": "/nowhere", "GIT_DIR": "/nowhere"}):
            result = host.call(invocation, "targets", {"branches": None})
        self.assertEqual(result[0]["key"], "probe")
        report = self.reported()
        environ = report["environ"]
        self.assertEqual(set(environ) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"},
                         {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONHASHSEED", "PYTHONSAFEPATH",
                          "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONPATH"})
        self.assertEqual(environ["PATH"], f"/usr/bin:/bin:{os.path.dirname(sys.executable)}")
        self.assertEqual(environ["LANG"], "C.UTF-8")
        self.assertEqual(environ["PYTHONHASHSEED"], "0")
        self.assertEqual(environ["HOME"], str(Path(environ["TMPDIR"]) / "home"))
        self.assertEqual(report["tmpdir"], environ["TMPDIR"])
        self.assertEqual(report["cwd"], str(Path(environ["TMPDIR"]).resolve()))
        self.assertEqual(environ["PYTHONPATH"], host.adapter_pythonpath(invocation))
        self.assertEqual(report["helper"], "imported through PYTHONPATH")
        self.assertFalse(report["api"])
        self.assertNotIn("leak", json.dumps(environ))
        self.assertFalse(Path(environ["TMPDIR"]).exists(), "the call directory is removed afterwards")

    def test_pythonpath_is_kit_source_then_repository_entries(self) -> None:
        invocation = self.invocation()
        entries = host.adapter_pythonpath(invocation).split(":")
        self.assertEqual(entries[0], str(invocation.kit_src))
        self.assertEqual(entries[1:], [str(self.mod.root / "scripts/pages"), str(self.mod.root / "e2e")])

    def test_pythonpath_refuses_a_symlinked_entry(self) -> None:
        shutil.rmtree(self.mod.root / "e2e")
        os.symlink(self.mod.root / "scripts", self.mod.root / "e2e")
        invocation = build_invocation(self.mod.root, None, support.environment(self.mod), check_repository=False)
        with self.assertRaisesRegex(MbError, "symlink"):
            host.adapter_pythonpath(invocation)

    def test_dot_python_path_is_the_repository_root(self) -> None:
        set_config(self.mod.root, python_path=["."])
        entries = host.adapter_pythonpath(self.invocation()).split(":")
        self.assertEqual(entries[1:], [str(self.mod.root)])

    def test_child_environment_grants_the_token_only_to_declared_network_hooks_in_token_jobs(self) -> None:
        tmp = self.directory / "tmp"
        base = {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONHASHSEED", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE",
                "PYTHONNOUSERSITE", "PYTHONPATH"}
        token = {"GH_TOKEN", "GITHUB_API_URL", "GITHUB_REPOSITORY"}
        for hook in sorted(protocol.NETWORK_HOOKS):
            for job in sorted(protocol.TOKEN_JOBS):
                environment = host.child_environment(self.invocation(job=job, token="t"), hook, tmpdir=tmp)
                granted = job in protocol.HOOK_JOBS[hook]
                self.assertEqual(set(environment), base | token if granted else base, (hook, job))
                if granted:
                    self.assertEqual(environment["GITHUB_API_URL"], "https://api.github.com")
                    self.assertEqual(environment["GH_TOKEN"], "t")
        cases = [("targets", "collect", None), ("collect", "collect", None),
                 ("authenticate_extensions", "prepare-pages-evidence", None),
                 ("authenticate_extensions", "refresh", None), ("authenticate_extensions", None, None),
                 # a mod-owned job that happens to be called "collect" is not the Pages callee job
                 ("authenticate_extensions", "collect", support.SOURCE_WORKFLOW)]
        for hook, job, workflow in cases:
            environment = host.child_environment(self.invocation(job=job, token="t", workflow=workflow), hook,
                                                 tmpdir=tmp)
            self.assertEqual(set(environment), base, (hook, job, workflow))
        foreign = self.invocation(job="collect", token="t",
                                  GITHUB_WORKFLOW_REF=f"The-Plum-Team/other/{PAGES_WORKFLOW_PATH}@refs/heads/master")
        self.assertEqual(set(host.child_environment(foreign, "authenticate_extensions", tmpdir=tmp)), base,
                         "another repository's pages.yml is not this mod's Pages job")
        set_config(self.mod.root, network_hooks=["verify_publication"])
        environment = host.child_environment(self.invocation(job="collect", token="t"), "authenticate_extensions",
                                             tmpdir=tmp)
        self.assertEqual(set(environment), base, "an undeclared network hook never gets the token")

    def test_child_argv_is_the_fixed_allowlist(self) -> None:
        invocation = self.invocation()
        argv = host.child_argv(invocation, "targets", request=self.directory / "req.json",
                               response=self.directory / "resp.json")
        self.assertEqual(argv, [sys.executable, "-P", "-m", "mod_base.adapter.host_child",
                                "--adapter", str(self.mod.root / "scripts/pages/mod_base_adapter.py"),
                                "--hook", "targets", "--request", str(self.directory / "req.json"),
                                "--response", str(self.directory / "resp.json")])
        for hook in ("synthesize", "unknown", "__init__"):
            with self.assertRaises(MbError):
                host.child_argv(invocation, hook, request=self.directory / "r", response=self.directory / "s")
        self.mode("ok")
        host.call(invocation, "targets", {"branches": None})
        report = self.reported()
        call_directory = Path(report["environ"]["TMPDIR"]).parent
        self.assertEqual(report["argv"], [*argv[4:9], str(call_directory / "req.json"), "--response",
                                          str(call_directory / "resp.json")])

    def test_fixture_hooks_never_go_through_the_host(self) -> None:
        with self.assertRaises(MbError):
            host.call(self.invocation(), "synthesize", {})


class TokenGatingTest(HostTestCase):
    def test_network_hook_in_a_token_job_receives_a_read_only_api(self) -> None:
        self.mode("ok")
        for hook, arguments in (("authenticate_extensions", {"manifest": {}, "extensions": {}}),
                                ("verify_publication", {"promotion_draft": PROMOTION_DRAFT})):
            job = "build" if hook == "verify_publication" else "collect"
            host.call(self.invocation(job=job, token="t0ken"), hook, arguments, network=True)
            report = self.reported()
            self.assertEqual(report["environ"]["GH_TOKEN"], "t0ken")
            self.assertEqual(report["environ"]["GITHUB_REPOSITORY"], self.mod.repository)
            self.assertTrue(report["api"])
            self.assertIs(report["api_writable"], False)

    def test_network_false_withholds_the_token_even_where_it_could_be_granted(self) -> None:
        self.mode("ok")
        host.call(self.invocation(job="collect", token="t0ken"), "authenticate_extensions",
                  {"manifest": {}, "extensions": {}})
        report = self.reported()
        self.assertNotIn("GH_TOKEN", report["environ"])
        self.assertFalse(report["api"])

    def test_network_is_refused_before_the_child_starts(self) -> None:
        self.mode("ok")
        refused = [
            ("targets", "collect", "t", {"branches": None}),  # not a network hook
            ("authenticate_extensions", "prepare-pages-evidence", "t", {"manifest": {}, "extensions": {}}),
            ("authenticate_extensions", None, "t", {"manifest": {}, "extensions": {}}),
            ("authenticate_extensions", "collect", None, {"manifest": {}, "extensions": {}}),  # no token
        ]
        for hook, job, token, arguments in refused:
            with self.subTest(hook=hook, job=job, token=token):
                with self.assertRaises(MbError):
                    host.call(self.invocation(job=job, token=token), hook, arguments, network=True)
        set_config(self.mod.root, network_hooks=["verify_publication"])
        with self.assertRaisesRegex(MbError, "network access"):
            host.call(self.invocation(job="collect", token="t"), "authenticate_extensions",
                      {"manifest": {}, "extensions": {}}, network=True)
        self.assertFalse(self.report.exists(), "no child ran")

    def test_a_network_hook_gets_the_pages_request_budget(self) -> None:
        self.mode("ok")
        host.call(self.invocation(job="collect", token="t0ken"), "authenticate_extensions",
                  {"manifest": {}, "extensions": {}}, network=True)
        self.assertEqual(self.reported()["api_budget"], lim.MAX_PAGES_API_READS)

    def test_no_hook_ever_runs_in_a_forbidden_job(self) -> None:
        self.mode("ok")
        for job in sorted(protocol.FORBIDDEN_JOBS):
            with self.subTest(job=job), self.assertRaisesRegex(MbError, "may never run"):
                host.call(self.invocation(job=job), "targets", {"branches": None})
        self.assertFalse(self.report.exists())


class PlacementTest(HostTestCase):
    """SPEC §4.3: every hook runs only in the jobs of its row, checked before any child starts."""

    ARGUMENTS = {
        "targets": {"branches": None},
        "compose": {"key": "mc1.20.1", "selected_compact_dir": "/a", "output_dir": "/b"},
        "family_validate": {"family": "mod-compatibility", "key": "mc1.20.1", "bundle_dir": "/a",
                            "expected_coverage_sha": "a" * 40, "output_dir": "/b"},
        "verify_publication": {"promotion_draft": PROMOTION_DRAFT},
        "authenticate_extensions": {"manifest": {}, "extensions": {}},
    }

    def test_placement_names_pages_jobs_only_under_pages_yml(self) -> None:
        for job in sorted(protocol.TOKEN_JOBS):
            self.assertEqual(host.placement(self.invocation(job=job)), job)
            self.assertEqual(host.placement(self.invocation(job=job, workflow=support.SOURCE_WORKFLOW)),
                             protocol.PREPARE_EVIDENCE)
        for job in (None, "prepare-pages-evidence", "public-evidence"):
            self.assertEqual(host.placement(self.invocation(job=job)), protocol.PREPARE_EVIDENCE)
        malformed = self.invocation(job="collect", GITHUB_WORKFLOW_REF="not a workflow ref")
        self.assertEqual(host.placement(malformed), protocol.PREPARE_EVIDENCE)

    def test_hooks_are_refused_outside_their_row(self) -> None:
        self.mode("ok")
        refused = [("compose", "build"), ("compose", "admit"), ("compose", None), ("family_validate", "collect"),
                   ("family_validate", None), ("verify_publication", "collect"), ("targets", "family"),
                   ("authenticate_extensions", "admit"), ("authenticate_extensions", None)]
        for hook, job in refused:
            with self.subTest(hook=hook, job=job), self.assertRaisesRegex(MbError, "may not run in"):
                host.call(self.invocation(job=job), hook, self.ARGUMENTS[hook])
        for hook, job, arguments in (("collect", "build", {"runtime_root": "/r"}),
                                     ("anchor_selection", "collect", {}),
                                     ("expected_source_jobs", None, {})):
            with self.subTest(hook=hook, job=job), self.assertRaisesRegex(MbError, "may not run in"):
                host.check_placement(self.invocation(job=job), hook)
        self.assertFalse(self.report.exists(), "no child ran")

    def test_a_mod_job_named_like_a_pages_job_is_the_prepare_evidence_composite(self) -> None:
        self.mode("ok")
        mod_build = self.invocation(job="build", token="t", workflow=support.SOURCE_WORKFLOW)
        for hook in ("targets", "collect", "anchor_selection", "expectation"):
            host.check_placement(mod_build, hook)
        with self.assertRaisesRegex(MbError, "may not run in 'prepare-evidence'"):
            host.call(mod_build, "verify_publication", self.ARGUMENTS["verify_publication"], network=True)
        host.call(mod_build, "targets", self.ARGUMENTS["targets"])
        self.assertNotIn("GH_TOKEN", self.reported()["environ"])
        self.assertEqual(set(host.child_environment(mod_build, "verify_publication", tmpdir=self.directory))
                         & {"GH_TOKEN", "GITHUB_API_URL"}, set())


class ChildFailureTest(HostTestCase):
    def test_hook_exception_becomes_one_bounded_line(self) -> None:
        self.mode("raise")
        with self.assertRaises(HookFailed) as caught:
            host.call(self.invocation(), "targets", {"branches": None})
        message = str(caught.exception)
        self.assertIn("ValueError: boom second line", message)
        self.assertFalse(any(ord(character) < 32 for character in message))

    def test_system_exit_in_a_hook_is_a_failure(self) -> None:
        self.mode("exit")
        with self.assertRaises(HookFailed):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_result_schema_violation_is_rejected(self) -> None:
        self.mode("unknown-key")
        with self.assertRaisesRegex(HookFailed, "surprise"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_arguments_are_validated_before_the_child_starts(self) -> None:
        self.mode("ok")
        with self.assertRaises(DocumentError):
            host.call(self.invocation(), "targets", {"branches": None, "extra": 1})
        with self.assertRaises(DocumentError):
            host.call(self.invocation(), "targets", {"branches": [{"name": "../x", "commit": "a" * 40,
                                                                   "tree": "b" * 40}]})
        self.assertFalse(self.report.exists())

    def test_missing_hook_is_unsupported(self) -> None:
        self.mode("ok")
        with self.assertRaises(HookUnsupported):
            host.call(self.invocation(job="collect"), "compose", {"key": "mc1.20.1", "selected_compact_dir": "/a",
                                                                  "output_dir": "/b"})

    def test_oversized_response_is_refused(self) -> None:
        self.mode("oversize")
        with self.assertRaisesRegex(StrictJsonError, "size"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_duplicate_key_response_is_refused(self) -> None:
        self.mode("duplicate-key")
        with self.assertRaisesRegex(StrictJsonError, "duplicate"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_crash_without_response_fails_closed(self) -> None:
        self.mode("crash")
        with self.assertRaisesRegex(MbError, "status 5"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_the_output_tail_is_quoted_on_one_line(self) -> None:
        self.mode("noisy-crash")
        with self.assertRaisesRegex(MbError, "status 5 without a valid response: first words last words$"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_unbounded_output_kills_the_child(self) -> None:
        self.mode("chatty")
        started = time.monotonic()
        with self.assertRaisesRegex(MbError, f"more than {host.MAX_CHILD_OUTPUT_BYTES} bytes of output"):
            host.call(self.invocation(), "targets", {"branches": None})
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("mb-hook-*/child.log")), [], "no output file is kept")

    def test_a_planted_response_is_never_trusted(self) -> None:
        self.mode("plant")
        with self.assertRaises(MbError):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_adapter_api_outside_the_window_is_refused(self) -> None:
        path = self.mod.root / "scripts/pages/mod_base_adapter.py"
        path.write_text(PROBE.replace("ADAPTER_API = 1", "ADAPTER_API = 7"), encoding="utf-8")
        self.mode("ok")
        with self.assertRaisesRegex(HookFailed, "ADAPTER_API"):
            host.call(self.invocation(), "targets", {"branches": None})

    def test_processes_left_behind_by_a_hook_are_killed(self) -> None:
        pid_file = self.directory / "pid"
        self.mode("grandchild", pid=str(pid_file))
        host.call(self.invocation(), "targets", {"branches": None})
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the hook's background process outlived the call")


class TimeoutTest(HostTestCase):
    timeout_seconds = 1

    def test_timeout_kills_the_child(self) -> None:
        self.mode("sleep")
        started = time.monotonic()
        with self.assertRaisesRegex(MbError, "timed out after 1 seconds"):
            host.call(self.invocation(), "targets", {"branches": None})
        self.assertLess(time.monotonic() - started, 30)


class ChildProgramTest(unittest.TestCase):
    def test_main_accepts_only_the_exact_argv_shape(self) -> None:
        good = ["--adapter", "/a.py", "--hook", "targets", "--request", "/r.json", "--response", "/s.json"]
        bad = [
            [],
            good[:6],
            good + ["--extra", "x"],
            good[2:4] + good[:2] + good[4:],
            ["--adapter", "a.py", *good[2:]],
            [*good[:3], "synthesize", *good[4:]],
            [*good[:3], "unknown", *good[4:]],
        ]
        stderr = mock.patch("sys.stderr", new_callable=lambda: open(os.devnull, "w"))
        with stderr as stream:
            for argv in bad:
                with self.subTest(argv=argv):
                    self.assertEqual(host_child.main(argv), 2)
            stream.close()


class InProcessDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-dispatch-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.mod = support.materialize("qs_like", self.directory / "repo")
        self.invocation = support.invocation(self.mod, support.environment(self.mod))
        self.context = adapter_api.Context(repo_root=self.mod.root, config=self.invocation.config,
                                           tmpdir=self.directory, implementation_sha=self.mod.commit)

    def test_unknown_hook_raises_before_touching_the_module(self) -> None:
        class Untouchable(types.ModuleType):
            def __getattribute__(self, name: str):
                raise AssertionError(f"module attribute {name} was read")

        for hook in ("unknown", "__init__", 3):
            with self.assertRaises(MbError):
                host_child.run_hook(self.context, Untouchable("x"), hook, {})

    def test_fixture_hook_needs_an_image_factory(self) -> None:
        module = host_child.load_adapter(self.mod.root / "scripts/pages/mod_base_fixtures.py")
        with self.assertRaisesRegex(MbError, "image_factory"):
            host_child.run_hook(self.context, module, "synthesize", {})

    def test_run_hook_validates_arguments_and_results(self) -> None:
        module = types.ModuleType("probe")
        module.ADAPTER_API = 1
        module.targets = lambda ctx, branches: [{"key": "Bad Key"}]
        with self.assertRaises(DocumentError):
            host_child.run_hook(self.context, module, "targets", {"branches": None})
        with self.assertRaises(DocumentError):
            host_child.run_hook(self.context, module, "targets", {"branches": None, "x": 1})
        with self.assertRaises(HookUnsupported):
            host_child.run_hook(self.context, module, "compose",
                                {"key": "mc1.20.1", "selected_compact_dir": "/a", "output_dir": "/b"})
        module.ADAPTER_API = True
        with self.assertRaisesRegex(MbError, "ADAPTER_API"):
            host_child.run_hook(self.context, module, "targets", {"branches": None})

    def test_hook_cannot_mutate_the_callers_arguments(self) -> None:
        module = types.ModuleType("probe")
        module.ADAPTER_API = 1

        def targets(ctx, branches):
            branches.append({"name": "evil", "commit": "a" * 40, "tree": "b" * 40})
            return [{"key": "k1", "label": "K", "matrix_sha256": "0" * 64, "contract_sha256": "0" * 64,
                     "subject": {"branch": "main", "commit": "a" * 40, "tree": "b" * 40}}]

        module.targets = targets
        branches = [{"name": "main", "commit": "a" * 40, "tree": "b" * 40}]
        host_child.run_hook(self.context, module, "targets", {"branches": branches})
        self.assertEqual(len(branches), 1)

    def test_load_adapter_uses_a_private_module_name(self) -> None:
        path = self.mod.root / "scripts/pages/mod_base_adapter.py"
        module = host_child.load_adapter(path)
        self.assertTrue(module.__name__.startswith("_mod_base_hook_module_"))
        self.assertNotIn("mod_base_adapter", sys.modules)
        link = self.directory / "link.py"
        os.symlink(path, link)
        with self.assertRaises(MbError):
            host_child.load_adapter(link)
        with self.assertRaises(MbError):
            host_child.load_adapter(Path("scripts/pages/mod_base_adapter.py"))


class ContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-context-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.mod = support.materialize("qs_like", self.directory / "repo")
        invocation = support.invocation(self.mod, support.environment(self.mod))
        self.context = adapter_api.Context(repo_root=self.mod.root, config=invocation.config,
                                           tmpdir=self.directory, implementation_sha=self.mod.commit)

    def test_read_blob_reads_exactly_one_bounded_inert_blob(self) -> None:
        expected = (self.mod.root / "release/release-matrix.json").read_bytes()
        self.assertEqual(self.context.read_blob(self.mod.commit, "release/release-matrix.json", 1 << 20), expected)
        for commit, path, limit in ((self.mod.commit, "release/missing.json", 1 << 20),
                                    (self.mod.commit, "release", 1 << 20),
                                    (self.mod.commit, "release/release-matrix.json", 16),
                                    ("f" * 40, "release/release-matrix.json", 1 << 20),
                                    ("HEAD", "release/release-matrix.json", 1 << 20),
                                    (self.mod.commit, "../release/release-matrix.json", 1 << 20),
                                    (self.mod.commit, ".git/config", 1 << 20),
                                    (self.mod.commit, "release/release-matrix.json", 0)):
            with self.subTest(commit=commit, path=path, limit=limit), self.assertRaises(MbError):
                self.context.read_blob(commit, path, limit)

    def test_read_blob_ignores_replacement_refs_and_the_working_tree(self) -> None:
        original = self.context.read_blob(self.mod.commit, "release/release-matrix.json", 1 << 20)
        blob = support.git(self.mod.root, "rev-parse", f"{self.mod.commit}:release/release-matrix.json")
        (self.directory / "evil.json").write_text('{"evil": true}\n')
        evil = support.git(self.mod.root, "hash-object", "-w", str(self.directory / "evil.json"))
        support.git(self.mod.root, "replace", blob, evil)
        (self.mod.root / "release/release-matrix.json").write_text("{}")
        self.assertEqual(self.context.read_blob(self.mod.commit, "release/release-matrix.json", 1 << 20), original)

    def test_runtime_tree_is_bounded_and_refuses_symlinks(self) -> None:
        root = self.directory / "runtime"
        (root / "a").mkdir(parents=True)
        (root / "a/result.json").write_text('{"x": 1}')
        (root / "a/dup.json").write_text('{"x": 1, "x": 2}')
        tree = self.context.runtime_tree(root)
        self.assertEqual(tree.files(), ["a/dup.json", "a/result.json"])
        self.assertTrue(tree.exists("a/result.json"))
        self.assertFalse(tree.exists("a/missing.json"))
        self.assertFalse(tree.exists("a"))
        self.assertEqual(tree.read_json("a/result.json", max_bytes=100), {"x": 1})
        self.assertEqual(tree.path("a/result.json"), root / "a/result.json")
        with self.assertRaises(MbError):
            tree.read_json("a/dup.json", max_bytes=100)
        with self.assertRaises(MbError):
            tree.read_bytes("a/result.json", max_bytes=3)
        os.symlink(root / "a", root / "link")
        with self.assertRaises(TreeError):
            tree.exists("link/result.json")
        with self.assertRaises(TreeError):
            tree.read_bytes("link/result.json", max_bytes=100)
        with self.assertRaises(TreeError):
            tree.path("link/result.json")
        with self.assertRaises(TreeError):
            self.context.runtime_tree(root).files()
        with self.assertRaises(TreeError):
            tree.exists("../outside")
        with self.assertRaises(TreeError):
            adapter_api.RuntimeTree(root / "link", max_files=10, max_total_bytes=100)
        bounded = adapter_api.RuntimeTree(root / "a", max_files=1, max_total_bytes=1000)
        with self.assertRaises(TreeError):
            bounded.files()

    def test_image_metrics_uses_the_kit_inspection(self) -> None:
        path = self.directory / "image.png"
        path.write_bytes(pattern_png(64, 36, 1))
        metrics = self.context.image_metrics(path, ("exact", 64, 36))
        self.assertEqual(metrics, self.context.image_metrics(str(path), SizePolicy.exact(64, 36)))
        self.assertEqual((metrics["width"], metrics["height"]), (64, 36))
        with self.assertRaises(ImageError):
            self.context.image_metrics(path, ("exact", 65, 36))
        with self.assertRaises(MbError):
            self.context.image_metrics(path, ("bogus", 64, 36))


if __name__ == "__main__":
    unittest.main()
