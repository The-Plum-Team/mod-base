"""The composites' credential boundary and the tools they run (SPEC §1.4, §5.6).

* Only the kit tree check (``setup``, ``prepare-evidence``, ``publish-family``) and the
  ``notify-pages`` dispatch receive ``GH_TOKEN``; every other step unsets every credential first,
  and no composite takes a token input or reads a secret.
* ``tools/verify_action_tree.py``: the executing kit must be exactly the tree the checked-out mod
  pins (exit 78 otherwise), read through the trees API with a bounded, fail-closed listing.
* ``notify-pages``: regex-validated inputs and the exact ``workflow_dispatch`` request, sent
  through ``tools/github_api_retry.sh`` with 10 attempts and a 60 s maximum delay.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mod_base
from mod_base.errors import ControllerSkew, MbError
from mod_base.github.fake import FakeGitHub
from tests.test_workflow_policy import (COMPOSITES, ROOT, SCRUB, SCRUB_API, TOKEN_VALUE, VERIFYING_COMPOSITES,
                                        ShellHarness, composite, outputs, parse_kit_argv, render_caller,
                                        require_tools, step)

TOOL = ROOT / "tools/verify_action_tree.py"
PIN = "0123456789abcdef0123456789abcdef01234567"
TREE = "fedcba9876543210fedcba9876543210fedcba98"
REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"


def _load_tool():
    spec = importlib.util.spec_from_file_location("mb_verify_action_tree", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verify_action_tree = _load_tool()


class CredentialBoundaryTests(unittest.TestCase):
    def test_only_the_tree_check_and_the_dispatch_hold_a_token(self) -> None:
        holders = {}
        for name in COMPOSITES:
            for item in composite(name)["runs"]["steps"]:
                env = item.get("env", {})
                if "GH_TOKEN" in env:
                    holders.setdefault(name, []).append(item["name"])
                    self.assertEqual(env["GH_TOKEN"], TOKEN_VALUE)
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertNotIn("token", json.dumps(item.get("with", {})).lower())
        self.assertEqual(holders, {
            **{name: ["Verify the executing kit against the checked-out pin"] for name in VERIFYING_COMPOSITES},
            "notify-pages": ["Dispatch the protected Pages publication"],
        })

    def test_every_step_scrubs_before_it_runs(self) -> None:
        for name in COMPOSITES:
            for item in composite(name)["runs"]["steps"]:
                if "run" not in item:
                    continue
                lines = item["run"].splitlines()
                with self.subTest(composite=name, step=item["name"]):
                    self.assertEqual(lines[0], "set -euo pipefail")
                    self.assertEqual(lines[1], SCRUB_API if "GH_TOKEN" in item.get("env", {}) else SCRUB)

    def test_no_token_or_secret_can_be_passed_in(self) -> None:
        for name in COMPOSITES:
            document = composite(name)
            text = (ROOT / f"actions/{name}/action.yml").read_text(encoding="utf-8")
            with self.subTest(composite=name):
                self.assertFalse([key for key in document.get("inputs", {}) if "token" in key.lower()])
                self.assertNotIn("secrets", text)
                self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL:", text)
                for item in document["runs"]["steps"]:
                    for variable, value in item.get("env", {}).items():
                        self.assertRegex(value, r"^(\$\{\{ (inputs|steps)\.[a-z0-9_.-]+ \}\}|\$\{\{ github\.token \}\}|"
                                                r"\"?[0-9]+\"?|mb-handoff--\$\{\{ inputs\.key \}\}--a\$\{\{ "
                                                r"github\.run_attempt \}\})$", f"{name}/{variable}")

    def test_notify_pages_is_pure_bash_without_checkout(self) -> None:
        document = composite("notify-pages")
        steps = document["runs"]["steps"]
        self.assertEqual([item["name"] for item in steps],
                         ["Validate the wake inputs", "Dispatch the protected Pages publication"])
        text = (ROOT / "actions/notify-pages/action.yml").read_text(encoding="utf-8")
        for word in ("python", "actions/checkout", "uses:", "mod_base", "verify_action_tree"):
            self.assertNotIn(word, text)
        dispatch = step(steps, "Dispatch the protected Pages publication")
        self.assertEqual((dispatch["env"]["GITHUB_API_RETRY_ATTEMPTS"],
                          dispatch["env"]["GITHUB_API_RETRY_MAX_DELAY_SECONDS"]), ("10", "60"))
        self.assertIn('source "$GITHUB_ACTION_PATH/../../tools/github_api_retry.sh"', dispatch["run"])
        self.assertEqual(list(document["inputs"]), ["operation", "run-id", "sha", "family", "bundle-key", "artifact-id",
                                                    "artifact-digest", "coverage-sha"])

    def test_the_setup_composite_installs_imaging_only_on_request(self) -> None:
        steps = composite("setup")["runs"]["steps"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1]["if"], "inputs.install-imaging == 'true'")
        self.assertIn('--requirement "$GITHUB_ACTION_PATH/../../requirements/pillow.txt"', steps[1]["run"])
        self.assertEqual(composite("setup")["inputs"]["install-imaging"]["default"], "true")
        self.assertEqual(composite("setup")["inputs"]["mod-root"]["default"], ".")


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


class VerifyActionTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="verify action tree ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kit = self.root / "kit"
        files = {
            "src/mod_base/__init__.py": b"x = 1\n", "src/mod_base/deep/y.py": b"y = 2\n",
            "site/index.html": b"<!doctype html>\n", "requirements/pillow.txt": b"Pillow==12.3.0\n",
            "tools/verify_action_tree.py": b"# tool\n", "tools/github_api_retry.sh": b"# retry\n",
            "actions/setup/action.yml": b"name: setup\n", "actions/notify-pages/action.yml": b"name: notify\n",
            "README.md": b"# kit\n", "tests/test_x.py": b"pass\n",
        }
        for relative, data in files.items():
            path = self.kit / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.mod = self.root / "mod"
        (self.mod / ".github/workflows").mkdir(parents=True)
        (self.mod / ".github/workflows/pages.yml").write_text(render_caller(PIN, "v1.0.0"))

    def pinned_tree(self, kit: Path | None = None) -> list[dict]:
        kit = kit or self.kit
        rows, directories = [], set()
        for path in sorted(kit.rglob("*")):
            relative = path.relative_to(kit).as_posix()
            if path.is_dir():
                directories.add(relative)
                continue
            rows.append({"path": relative, "mode": "100644", "type": "blob", "sha": git_blob_id(path.read_bytes()),
                         "size": path.stat().st_size})
        rows.extend({"path": directory, "mode": "040000", "type": "tree", "sha": "e" * 40} for directory in directories)
        return sorted(rows, key=lambda row: row["path"])

    def api(self, rows: list[dict], *, truncated: bool = False) -> FakeGitHub:
        fake = FakeGitHub(repository=mod_base.KIT_REPOSITORY)
        fake.add_commit(PIN, TREE)
        fake.add_tree(TREE, rows, truncated=truncated)
        return fake

    def test_the_pinned_tree_verifies(self) -> None:
        pin = verify_action_tree.verify(self.kit, "setup", self.mod, self.api(self.pinned_tree()))
        self.assertEqual((pin.sha, pin.version), (PIN, "v1.0.0"))

    def test_every_difference_in_the_verified_set_is_controller_skew(self) -> None:
        rows = self.pinned_tree()

        def changed(kit: Path) -> None:
            (kit / "src/mod_base/deep/y.py").write_bytes(b"y = 3\n")

        def extra(kit: Path) -> None:
            (kit / "site/extra.js").write_bytes(b"")

        def missing(kit: Path) -> None:
            (kit / "requirements/pillow.txt").unlink()

        def bytecode(kit: Path) -> None:
            (kit / "src/mod_base/__pycache__").mkdir()
            (kit / "src/mod_base/__pycache__/x.cpython-313.pyc").write_bytes(b"\x00")

        def symlink(kit: Path) -> None:
            (kit / "tools/link.sh").symlink_to(kit / "tools/github_api_retry.sh")

        def own_action(kit: Path) -> None:
            (kit / "actions/setup/action.yml").write_bytes(b"name: tampered\n")

        def tools(kit: Path) -> None:
            (kit / "tools/github_api_retry.sh").write_bytes(b"# tampered\n")

        for mutate in (changed, extra, missing, bytecode, symlink, own_action, tools):
            kit = self.root / f"kit-{mutate.__name__}"
            shutil.copytree(self.kit, kit, symlinks=True)
            mutate(kit)
            with self.subTest(case=mutate.__name__), self.assertRaises(ControllerSkew):
                verify_action_tree.verify(kit, "setup", self.mod, self.api(rows))

    def test_files_outside_the_verified_set_do_not_matter(self) -> None:
        rows = self.pinned_tree() + [{"path": "docs/notes with space.md", "mode": "100644", "type": "blob",
                                      "sha": "c" * 40},
                                     {"path": "canary/link", "mode": "120000", "type": "blob", "sha": "d" * 40}]
        (self.kit / "actions/notify-pages/action.yml").write_bytes(b"name: other composite\n")
        (self.kit / "tests/test_x.py").write_bytes(b"changed\n")
        (self.kit / "notes.txt").write_bytes(b"untracked\n")
        verify_action_tree.verify(self.kit, "setup", self.mod, self.api(rows))

    def test_a_malformed_or_incomplete_listing_is_rejected(self) -> None:
        rows = self.pinned_tree()
        link = [{**row, "mode": "120000"} if row["path"] == "tools/github_api_retry.sh" else row for row in rows]
        submodule = rows + [{"path": "src/vendor", "mode": "160000", "type": "commit", "sha": "d" * 40}]
        for label, listing, truncated in (("truncated", rows, True), ("symlink", link, False),
                                          ("submodule", submodule, False)):
            with self.subTest(case=label):
                with self.assertRaises(MbError) as caught:
                    verify_action_tree.verify(self.kit, "setup", self.mod, self.api(listing, truncated=truncated))
                self.assertNotIsInstance(caught.exception, ControllerSkew)
        with self.assertRaises(ControllerSkew):
            verify_action_tree.compare_inventories({}, {}, pin=PIN)
        with self.assertRaises(MbError):
            verify_action_tree.tree_inventory([{"path": "src/../x", "type": "blob", "mode": "100644", "sha": "a" * 40}],
                                              "setup")
        with self.assertRaises(MbError):
            duplicate = {"path": "src/a", "type": "blob", "mode": "100644", "sha": "a" * 40}
            verify_action_tree.tree_inventory([duplicate, duplicate], "setup")

    def test_the_mod_must_carry_exactly_one_pin(self) -> None:
        rows = self.pinned_tree()
        workflows = self.mod / ".github/workflows"
        (workflows / "other.yml").write_text(
            "jobs:\n  x:\n    steps:\n      - uses: The-Plum-Team/mod-base/actions/setup@"
            + "9" * 40 + " # v1.0.1\n")
        with self.assertRaises(MbError):
            verify_action_tree.verify(self.kit, "setup", self.mod, self.api(rows))
        shutil.rmtree(workflows)
        with self.assertRaises(MbError):
            verify_action_tree.verify(self.kit, "setup", self.mod, self.api(rows))

    def test_run_binds_the_executing_action_and_exports_the_kit(self) -> None:
        local = verify_action_tree.local_inventory(ROOT, "setup")
        rows = [{"path": path, "mode": "100644", "type": "blob", "sha": oid} for path, oid in local.items()]
        github_env = self.root / "github_env"
        environ = {"GITHUB_ACTION_PATH": str(ROOT / "actions/setup"), "GITHUB_ENV": str(github_env)}
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(verify_action_tree.run(["--mod-root", str(self.mod)], environ, api=self.api(rows)), 0)
        self.assertEqual(printed.getvalue(), f"verified the executing mod-base kit v1.0.0 at {PIN} (setup)\n")
        self.assertEqual(outputs(github_env), {"MOD_BASE_KIT_PATH": str(ROOT), "MOD_BASE_KIT_SHA": PIN,
                                               "MOD_BASE_KIT_VERSION": "v1.0.0"})
        with self.assertRaises(ControllerSkew):
            verify_action_tree.run(["--mod-root", str(self.mod)],
                                   {**environ, "GITHUB_ACTION_PATH": str(self.root / "kit/actions/setup")},
                                   api=self.api(rows))
        with self.assertRaises(MbError):
            verify_action_tree.run(["--mod-root", str(self.mod)],
                                   {**environ, "GITHUB_ACTION_PATH": str(ROOT / "actions/notify-pages")},
                                   api=self.api(rows))
        for missing in ("GITHUB_ACTION_PATH", "GITHUB_ENV"):
            with self.assertRaises(MbError):
                verify_action_tree.run(["--mod-root", str(self.mod)],
                                       {key: value for key, value in environ.items() if key != missing},
                                       api=self.api(rows))
        with self.assertRaises(MbError):
            verify_action_tree.run(["--mod-root", str(self.mod)], environ)

    def test_process_exit_codes(self) -> None:
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT / "src"),
                "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_ENV": str(self.root / "env"), "GH_TOKEN": "unused"}
        command = [sys.executable, "-P", str(TOOL), "--mod-root", str(self.mod)]
        skew = subprocess.run(command, env={**base, "GITHUB_ACTION_PATH": str(self.root / "elsewhere/actions/setup")},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(skew.returncode, 78, skew.stderr)
        self.assertEqual(len(skew.stderr.strip().splitlines()), 1)
        rejected = subprocess.run(command, env=base, capture_output=True, text=True, timeout=60)
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        foreign = subprocess.run(command, env={**base, "GITHUB_ACTION_PATH": str(ROOT / "actions/setup"),
                                               "GITHUB_API_URL": "https://ghe.example.com/api/v3"},
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(foreign.returncode, 2, foreign.stderr)


class CompositeShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="composite shell ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.runner_temp = self.root / "runner temp"
        self.runner_temp.mkdir()

    def run_step(self, name: str, step_name: str, env: dict[str, str], *, fixtures: dict | None = None,
                 cwd: Path | None = None):
        output = self.root / "github_output"
        if output.exists():
            output.unlink()
        base = {"GITHUB_ACTION_PATH": str(ROOT / f"actions/{name}"), "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(self.runner_temp), "GITHUB_REPOSITORY": REPOSITORY,
                "STUB_PYTHON3_SCRIPT": "raise SystemExit(int(os.environ.get('STUB_EXIT', '0')))"}
        result = self.harness.run(step(composite(name)["runs"]["steps"], step_name)["run"], {**base, **env},
                                  fixtures=fixtures, cwd=cwd, record_env=("GH_TOKEN", "PYTHONPATH"))
        return result, outputs(output), self.harness.records()

    # -- notify-pages ----------------------------------------------------------------------------

    def notify_inputs(self, operation: str) -> dict[str, str]:
        values = {"OPERATION": operation, "RUN_ID": "36042781699", "SHA": "a" * 40, "FAMILY": "", "BUNDLE_KEY": "",
                  "ARTIFACT_ID": "", "ARTIFACT_DIGEST": "", "COVERAGE_SHA": ""}
        if operation == "family":
            values.update(FAMILY="mod-compatibility", BUNDLE_KEY="mc1.20.1", ARTIFACT_ID="812345",
                          ARTIFACT_DIGEST="sha256:" + "c" * 64, COVERAGE_SHA="a" * 40)
        return values

    def test_notify_input_validation(self) -> None:
        for operation in ("deploy", "family"):
            result, _, records = self.run_step("notify-pages", "Validate the wake inputs",
                                               self.notify_inputs(operation))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(records, [])
        rejected = [
            ("rotate", {}), ("manual", {}), ("", {}), ("deploy", {"RUN_ID": "0"}), ("deploy", {"RUN_ID": "1\n2"}),
            ("deploy", {"SHA": "HEAD"}), ("deploy", {"FAMILY": "mod-compatibility"}),
            ("deploy", {"ARTIFACT_DIGEST": "sha256:" + "c" * 64}), ("family", {"FAMILY": "Mod"}),
            ("family", {"BUNDLE_KEY": "a--b"}), ("family", {"ARTIFACT_ID": ""}),
            ("family", {"ARTIFACT_DIGEST": "c" * 64}), ("family", {"COVERAGE_SHA": "a" * 41}),
        ]
        for operation, overrides in rejected:
            with self.subTest(operation=operation, overrides=overrides):
                values = {**self.notify_inputs(operation if operation in ("deploy", "family") else "deploy"),
                          "OPERATION": operation, **overrides}
                result, _, _ = self.run_step("notify-pages", "Validate the wake inputs", values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mod-base notify-pages: invalid", result.stderr)

    def dispatch(self, operation: str, response: object = None):
        fixtures = {f"GET repos/{REPOSITORY}": {"body": {"default_branch": "master"}},
                    f"POST repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches": response or {"body": None}}
        env = {**self.notify_inputs(operation), "GH_TOKEN": "t", "GITHUB_API_RETRY_ATTEMPTS": "10",
               "GITHUB_API_RETRY_MAX_DELAY_SECONDS": "60", "GITHUB_RUN_ID": "5"}
        return self.run_step("notify-pages", "Dispatch the protected Pages publication", env, fixtures=fixtures)

    def test_notify_dispatches_exactly_the_wake(self) -> None:
        result, _, records = self.dispatch("deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        posts = [record for record in records if record.get("route", "").startswith("POST")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]["input"]), {
            "ref": "master", "inputs": {"operation": "deploy", "run_id": "36042781699", "sha": "a" * 40}})
        self.assertEqual((posts[0]["token"], posts[0]["retry"]), ("t", ["10", "60"]))
        result, _, records = self.dispatch("family")
        self.assertEqual(result.returncode, 0, result.stderr)
        post = [record for record in records if record.get("route", "").startswith("POST")][0]
        self.assertEqual(json.loads(post["input"]), {"ref": "master", "inputs": {
            "operation": "family", "run_id": "36042781699", "sha": "a" * 40, "family": "mod-compatibility",
            "bundle_key": "mc1.20.1", "artifact_id": "812345", "artifact_digest": "sha256:" + "c" * 64,
            "coverage_sha": "a" * 40}})
        self.assertFalse([record for record in records if record["tool"] == "python3"])

    def test_notify_retries_transient_failures_up_to_ten_attempts(self) -> None:
        result, _, records = self.dispatch("deploy", [{"fail": "gh: Bad Gateway (HTTP 502)"}] * 3 + [{"body": None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(record.get("route", "").startswith("POST") for record in records), 4)
        result, _, records = self.dispatch("deploy", [{"fail": "gh: Bad Gateway (HTTP 502)"}] * 12)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(record.get("route", "").startswith("POST") for record in records), 10)
        result, _, records = self.dispatch("deploy", [{"fail": "gh: HTTP 422: Workflow does not have "
                                                               "'workflow_dispatch' trigger"}, {"body": None}])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(record.get("route", "").startswith("POST") for record in records), 1)
        result, _, records = self.dispatch("deploy", [{"fail": "gh: HTTP 429: secondary rate limit"}, {"body": None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        sleeps = [int(record["argv"][0]) for record in records if record["tool"] == "sleep"]
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 60)

    # -- the kit tree check and upload bindings ---------------------------------------------------

    def test_verifying_steps_validate_inputs_before_running_the_check(self) -> None:
        mod = {"MOD_ROOT": ".", "INSTALL_IMAGING": "false", "GH_TOKEN": "t", "GITHUB_TOKEN": "ambient"}
        result, _, records = self.run_step("setup", "Verify the executing kit against the checked-out pin", mod)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [record for record in records if record["tool"] == "python3"]
        self.assertEqual([call["argv"] for call in calls],
                         [["-P", f"{ROOT}/actions/setup/../../tools/verify_action_tree.py", "--mod-root", "."]])
        self.assertEqual(calls[0]["env"], {"GH_TOKEN": "t", "PYTHONPATH": f"{ROOT}/actions/setup/../../src"})
        for overrides in ({"MOD_ROOT": "/abs"}, {"MOD_ROOT": "../up"}, {"MOD_ROOT": "a/../../b"}, {"MOD_ROOT": ""},
                          {"INSTALL_IMAGING": "yes"}):
            with self.subTest(overrides=overrides):
                result, _, records = self.run_step("setup", "Verify the executing kit against the checked-out pin",
                                                   {**mod, **overrides})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse([record for record in records if record["tool"] == "python3"])
        prepare = {"MOD_ROOT": ".", "E2E_ROOT": "/runner/tmp/aggregate", "KEY": "mc1.20.1",
                   "SUBJECT_BRANCH": "master", "SUBJECT_COMMIT": "a" * 40, "SUBJECT_TREE": "b" * 40,
                   "TESTED_RUN_ID": "7", "TESTED_RUN_ATTEMPT": "2", "TESTED_BRANCH": "master",
                   "TESTED_COMMIT": "c" * 40, "TESTED_CONTROLLER_BRANCH": "master", "TESTED_CONTROLLER_SHA": "c" * 40,
                   "EXTENSIONS": "", "ANCHOR": "auto", "GH_TOKEN": "t"}
        verify = "Verify the executing kit against the checked-out pin"
        result, _, _ = self.run_step("prepare-evidence", verify, prepare)
        self.assertEqual(result.returncode, 0, result.stderr)
        for overrides in ({"KEY": "MC"}, {"SUBJECT_TREE": "x"}, {"TESTED_RUN_ATTEMPT": "0"}, {"ANCHOR": "on"},
                          {"SUBJECT_BRANCH": "a..b"}, {"E2E_ROOT": "x\ny"}, {"EXTENSIONS": "e\tx"}):
            with self.subTest(overrides=overrides):
                result, _, records = self.run_step("prepare-evidence", verify, {**prepare, **overrides})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse([record for record in records if record["tool"] == "python3"])

    def test_upload_identities_are_normalized_to_kit_digests(self) -> None:
        for name, step_name in (("prepare-evidence", "Bind the uploaded handoff identity"),
                                ("publish-family", "Bind the uploaded family handoff identity")):
            for digest in ("c" * 64, "sha256:" + "c" * 64):
                with self.subTest(composite=name, digest=digest):
                    result, values, _ = self.run_step(name, step_name, {"ARTIFACT_ID": "812345",
                                                                        "ARTIFACT_DIGEST": digest})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(values, {"artifact_id": "812345", "digest": "sha256:" + "c" * 64})
            for env in ({"ARTIFACT_ID": "", "ARTIFACT_DIGEST": "c" * 64},
                        {"ARTIFACT_ID": "1", "ARTIFACT_DIGEST": "sha1:" + "c" * 40},
                        {"ARTIFACT_ID": "1", "ARTIFACT_DIGEST": ""}):
                with self.subTest(composite=name, env=env):
                    result, values, _ = self.run_step(name, step_name, env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(values, {})

    def kit_argv(self, records: list[dict]) -> list[list[str]]:
        calls = []
        for record in records:
            if record["tool"] == "python3":
                self.assertEqual(record["argv"][:3], ["-P", "-m", "mod_base"])
                self.assertIsNone(record["env"]["GH_TOKEN"])
                parse_kit_argv(record["argv"][3:])
                calls.append(record["argv"][3:])
        return calls

    def test_prepare_and_fresh_validation_arguments(self) -> None:
        env = {"MOD_ROOT": "mod", "E2E_ROOT": "/tmp/e2e out", "KEY": "mc1.20.1", "SUBJECT_BRANCH": "master",
               "SUBJECT_COMMIT": "a" * 40, "SUBJECT_TREE": "b" * 40, "TESTED_RUN_ID": "7", "TESTED_RUN_ATTEMPT": "2",
               "TESTED_BRANCH": "master", "TESTED_COMMIT": "c" * 40, "TESTED_CONTROLLER_BRANCH": "master",
               "TESTED_CONTROLLER_SHA": "c" * 40, "EXTENSIONS": "", "ANCHOR": "auto", "GH_TOKEN": "leak"}
        result, values, records = self.run_step("prepare-evidence", "Prepare the SHA-bound evidence handoff", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        work = values["work"]
        self.assertTrue(work.startswith(str(self.runner_temp / "mod-base-prepare.")))
        self.assertEqual(values["handoff"], f"{work}/handoff")
        self.assertEqual(self.kit_argv(records), [[
            "prepare", "--repo", "mod", "--config", "mod/site/mod-base.json", "--e2e-root", "/tmp/e2e out", "--key",
            "mc1.20.1", "--output", f"{work}/handoff", "--subject-branch", "master", "--subject-commit", "a" * 40,
            "--subject-tree", "b" * 40, "--tested-run-id", "7", "--tested-run-attempt", "2", "--tested-branch",
            "master", "--tested-commit", "c" * 40, "--tested-controller-branch", "master", "--tested-controller-sha",
            "c" * 40, "--anchor", "auto"]])
        result, values, records = self.run_step("prepare-evidence", "Prepare the SHA-bound evidence handoff",
                                                 {**env, "EXTENSIONS": "out/extensions.json", "ANCHOR": "off"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_argv(records)[0][-4:], ["--anchor", "off", "--extensions", "out/extensions.json"])
        result, _, records = self.run_step("prepare-evidence", "Validate the handoff in a fresh process", {
            "MOD_ROOT": "mod", "KEY": "mc1.20.1", "SUBJECT_COMMIT": "a" * 40, "HANDOFF_DIR": "/w/handoff"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_argv(records), [[
            "validate", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--kind",
            "handoff", "--input", "/w/handoff", "--expected-subject-commit", "a" * 40]])
        result, _, records = self.run_step("publish-family", "Validate the family envelope in a fresh process", {
            "MOD_ROOT": ".", "KEY": "mc1.20.1", "HANDOFF_DIR": "/w/family"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_argv(records), [[
            "validate", "--repo", ".", "--config", "./site/mod-base.json", "--key", "mc1.20.1", "--kind", "family",
            "--input", "/w/family"]])

    def test_the_anchor_is_cut_from_the_uploaded_handoff_and_validated(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "mod/site").mkdir(parents=True)
        name = "mb-anchor--mc1.20.1--" + "a" * 40 + "--7--a2"
        script = f"""
if arguments[3:5] == ["anchor", "identity"]:
    eligible = os.environ.get("STUB_ELIGIBLE", "true") == "true"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("anchor_eligible=" + ("true" if eligible else "false") + "\\nanchor_name={name}\\n")
    chosen = json.loads(os.environ["STUB_NAME"]) if "STUB_NAME" in os.environ else "{name}"
    print(json.dumps({{"eligible": eligible, "name": chosen if eligible else None}}))
"""
        env = {"MOD_ROOT": "mod", "KEY": "mc1.20.1", "SUBJECT_COMMIT": "a" * 40, "WORK": "/w",
               "HANDOFF_DIR": "/w/handoff", "HANDOFF_NAME": "mb-handoff--mc1.20.1--a2", "HANDOFF_ID": "812345",
               "HANDOFF_DIGEST": "sha256:" + "d" * 64, "STUB_PYTHON3_SCRIPT": script}

        def run(retention: object, **extra: str):
            (workspace / "mod/site/mod-base.json").write_text(json.dumps({"anchor": {"retention_days": retention}}))
            return self.run_step("prepare-evidence", "Cut the lossless anchor from the uploaded handoff",
                                 {**env, **extra}, cwd=workspace)

        result, values, records = run(90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"anchor_eligible": "true", "anchor_name": name, "name": name, "path": "/w/anchor",
                                  "retention_days": "90"})
        raw = ["--raw-artifact-id", "812345", "--raw-artifact-name", "mb-handoff--mc1.20.1--a2",
               "--raw-artifact-digest", "sha256:" + "d" * 64]
        self.assertEqual(self.kit_argv(records), [
            ["anchor", "identity", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1",
             "--handoff", "/w/handoff"],
            ["anchor", "create", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1",
             "--handoff", "/w/handoff", *raw, "--output", "/w/anchor"],
            ["anchor", "validate", "--key", "mc1.20.1", "--input", "/w/anchor", "--expected-subject-commit", "a" * 40,
             *raw]])
        result, values, records = run(90, STUB_ELIGIBLE="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[:2] for call in self.kit_argv(records)], [["anchor", "identity"]])
        for retention in (0, 91, None):
            with self.subTest(retention=retention):
                result, values, _ = run(retention)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("retention_days", values)
        for bad in (None, 7, "", "mb-anchor--mc26.3--" + "a" * 40 + "--7--a2", name + "x",
                    "mb-anchor--mc1.20.1--" + "a" * 40 + "--0--a2", "mb-anchor--mc1.20.1--" + "a" * 40 + "--7",
                    "mb-handoff--mc1.20.1--a2"):
            with self.subTest(name=bad):
                result, values, records = run(90, STUB_NAME=json.dumps(bad))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("name", values)
                self.assertEqual([call[:2] for call in self.kit_argv(records)], [["anchor", "identity"]])

    def test_family_retention_comes_from_exactly_one_configured_family(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "site").mkdir(parents=True)
        env = {"MOD_ROOT": ".", "FAMILY": "mod-compatibility", "KEY": "mc1.20.1", "BUNDLE": "public-compatibility",
               "COVERAGE_SHA": "a" * 40, "SUBJECT_BRANCH": "master", "SUBJECT_COMMIT": "a" * 40}

        def run(families: list) -> tuple:
            (workspace / "site/mod-base.json").write_text(json.dumps({"families": families}))
            return self.run_step("publish-family", "Wrap the family bundle in its envelope", env, cwd=workspace)

        result, values, records = run([{"id": "mod-compatibility", "retention_days": 7}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["retention_days"], "7")
        self.assertTrue(values["handoff"].startswith(str(self.runner_temp / "mod-base-family.")))
        call = [record for record in records if record["tool"] == "python3"][0]
        parse_kit_argv(call["argv"][3:])
        self.assertEqual(call["argv"][3:], [
            "family", "envelope", "--repo", ".", "--config", "./site/mod-base.json", "--family", "mod-compatibility",
            "--key", "mc1.20.1", "--bundle", "public-compatibility", "--coverage-sha", "a" * 40, "--subject-branch",
            "master", "--subject-commit", "a" * 40, "--output", values["handoff"]])
        self.assertIsNone(call["env"]["GH_TOKEN"])
        for families in ([], [{"id": "mod-compatibility", "retention_days": 8}],
                         [{"id": "mod-compatibility", "retention_days": 7}] * 2,
                         [{"id": "other", "retention_days": 7}]):
            with self.subTest(families=families):
                result, values, _ = run(families)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("retention_days", values)


if __name__ == "__main__":
    unittest.main()
