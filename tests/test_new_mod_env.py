"""``tools/new_mod_env.py``: the owner's ``mod-base-gate`` setup for a new mod (docs/NEW-MOD.md).

The ``gh`` calls are answered by an in-memory repository; nothing reaches the network. The tests
pin the order (the default-branch restriction is written and read back before any credential),
the refusals (another branch, a reviewer or wait timer, a repository or organization secret of the
key's name, a branch other than the default) and that the key travels only on ``gh secret set``'s
standard input.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parents[1]
PROGRAM = KIT / "tools/new_mod_env.py"
spec = importlib.util.spec_from_file_location("new_mod_env_tests", PROGRAM)
new_mod_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(new_mod_env)

REPO = "The-Plum-Team/new-mod"
CLIENT = "Iv23liExampleClient0"
KEY = (b"-----BEGIN RSA PRIVATE KEY-----\n" + b"TUlJRXBRSUJBQUtDQVFFQXRlc3RrZXk=\n" * 3
       + b"-----END RSA PRIVATE KEY-----\n")
KEY_LINE = "TUlJRXBRSUJBQUtDQVFFQXRlc3RrZXk="
POLICIES = f"repos/{REPO}/environments/mod-base-gate/deployment-branch-policies"


class FakeGitHub:
    """Answers the ``gh`` invocations of the program from in-memory repository state."""

    def __init__(self, *, default: str = "main") -> None:
        self.default = default
        self.repo_secrets: list[str] = []
        self.org_secrets: list[str] = []
        self.environment: dict[str, Any] | None = None
        self.policies: list[dict[str, str]] = []
        self.variables: dict[str, str] = {}
        self.secrets: dict[str, bytes] = {}
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.ignore_put = False
        self.fail: str | None = None
        self.fail_detail = b"HTTP 502: Bad Gateway"
        self.app: dict[str, Any] = {"id": 5256389, "slug": "plum-mod-base-gate", "client_id": CLIENT}

    def configured(self, *policies: tuple[str, str], rules: Sequence[str] = ()) -> FakeGitHub:
        self.environment = {"name": "mod-base-gate", "deployment_branch_policy": dict(new_mod_env.BRANCH_POLICY),
                            "protection_rules": [{"type": kind} for kind in ("branch_policy", *rules)]}
        self.policies = [{"name": name, "type": kind} for name, kind in policies]
        return self

    def writes(self) -> list[list[str]]:
        return [arguments for arguments, _stdin in self.calls
                if arguments[0] in ("variable", "secret") or "-X" in arguments]

    def __call__(self, arguments: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        arguments = list(arguments)
        self.calls.append((arguments, stdin))
        if self.fail is not None and self.fail in " ".join(arguments):
            return 1, b"", self.fail_detail
        if arguments[0] == "variable":
            self.variables[arguments[2]] = arguments[arguments.index("--body") + 1]
            return 0, b"", b""
        if arguments[0] == "secret":
            self.secrets[arguments[2]] = stdin or b""
            return 0, b"", b""
        method, endpoint = "GET", arguments[-1]
        if "-X" in arguments:
            method, endpoint = arguments[2], arguments[3]
        path = endpoint.split("?", 1)[0]
        body: Any = None
        if method == "GET" and path == "apps/plum-mod-base-gate":
            body = self.app
        elif method == "GET" and path == f"repos/{REPO}":
            body = {"full_name": REPO, "default_branch": self.default}
        elif method == "GET" and path == f"repos/{REPO}/actions/secrets":
            body = {"total_count": len(self.repo_secrets), "secrets": [{"name": n} for n in self.repo_secrets]}
        elif method == "GET" and path == f"repos/{REPO}/actions/organization-secrets":
            body = {"total_count": len(self.org_secrets), "secrets": [{"name": n} for n in self.org_secrets]}
        elif method == "GET" and path == f"repos/{REPO}/environments":
            items = [] if self.environment is None else [self.environment]
            body = {"total_count": len(items) + 1, "environments": [{"name": "github-pages"}, *items]}
        elif method == "PUT" and path == f"repos/{REPO}/environments/mod-base-gate":
            requested = json.loads(stdin or b"{}")
            if not self.ignore_put:
                self.environment = self.environment or {"name": "mod-base-gate", "protection_rules": []}
                self.environment["deployment_branch_policy"] = requested["deployment_branch_policy"]
                self.environment["protection_rules"] = [r for r in self.environment["protection_rules"]
                                                        if r["type"] != "branch_policy"] + [{"type": "branch_policy"}]
            body = self.environment or {"name": "mod-base-gate"}
        elif method == "GET" and path == POLICIES:
            body = {"total_count": len(self.policies), "branch_policies": self.policies}
        elif method == "POST" and path == POLICIES:
            fields = dict(arguments[i + 1].split("=", 1) for i, a in enumerate(arguments) if a == "-f")
            self.policies.append({"name": fields["name"], "type": fields["type"]})
            body = self.policies[-1]
        elif method == "GET" and path.startswith(f"repos/{REPO}/environments/mod-base-gate/variables/"):
            name = path.rsplit("/", 1)[1]
            if name in self.variables:
                body = {"name": name, "value": self.variables[name]}
        elif method == "GET" and path.startswith(f"repos/{REPO}/environments/mod-base-gate/secrets/"):
            name = path.rsplit("/", 1)[1]
            if name in self.secrets:
                body = {"name": name}
        if body is None:
            return 1, b"", f"gh: Not Found (HTTP 404) {method} {path}".encode()
        return 0, json.dumps(body).encode(), b""


def private_file(path: Path, data: bytes) -> Path:
    """A file its owner alone can read: mode 0600 on POSIX; on Windows the temporary directory's
    inherited access list (the user, SYSTEM and Administrators)."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    return path


def share_with_everyone(path: Path) -> None:
    """Let every user of the machine read ``path``."""

    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    else:
        path.chmod(0o644)


class NewModEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key_file = private_file(self.root / "app.pem", KEY)
        self.app_json = private_file(self.root / "app.json", json.dumps(
            {"id": 1, "client_id": CLIENT, "pem": KEY.decode()}).encode("utf-8"))

    def run_program(self, github: FakeGitHub, *extra: str, credentials: Sequence[str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        if credentials is None:
            credentials = ["--client-id", CLIENT, "--key-file", str(self.key_file)]
        status = new_mod_env.main(["--repo", REPO, *credentials, *extra], run=github, out=out, err=err)
        output, errors = out.getvalue(), err.getvalue()
        for text in (output, errors, *(" ".join(arguments) for arguments, _stdin in github.calls)):
            self.assertNotIn(KEY_LINE, text)
            self.assertNotIn("PRIVATE KEY", text)
        return status, output, errors

    def assert_key_only_on_stdin(self, github: FakeGitHub) -> None:
        carriers = [arguments for arguments, stdin in github.calls if stdin and KEY_LINE.encode() in stdin]
        self.assertEqual(carriers, [["secret", "set", "MOD_BASE_GATE_APP_PRIVATE_KEY", "--repo", REPO,
                                     "--env", "mod-base-gate"]])

    def test_a_new_repository_gets_the_environment_then_the_credentials(self) -> None:
        github = FakeGitHub()
        status, output, errors = self.run_program(github)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.environment["deployment_branch_policy"],
                         {"protected_branches": False, "custom_branch_policies": True})
        self.assertEqual(github.policies, [{"name": "main", "type": "branch"}])
        self.assertEqual(github.variables, {"MOD_BASE_GATE_APP_CLIENT_ID": CLIENT})
        self.assertEqual(github.secrets, {"MOD_BASE_GATE_APP_PRIVATE_KEY": KEY})
        self.assert_key_only_on_stdin(github)
        writes = [" ".join(arguments[:4]) for arguments in github.writes()]
        self.assertEqual(writes, [f"api -X PUT repos/{REPO}/environments/mod-base-gate",
                                  f"api -X POST {POLICIES}",
                                  f"variable set MOD_BASE_GATE_APP_CLIENT_ID --repo",
                                  f"secret set MOD_BASE_GATE_APP_PRIVATE_KEY --repo"])
        index = {" ".join(a): i for i, (a, _s) in enumerate(github.calls)}
        reread = max(i for text, i in index.items() if text.startswith(f"api {POLICIES}?"))
        self.assertLess(reread, min(i for text, i in index.items() if text.startswith(("variable", "secret"))))
        self.assertIn("deployment branches: main only", output)
        self.assertIn("(value not shown)", output)

    def test_the_manifest_flow_json_is_accepted_and_a_rerun_rotates_without_a_second_policy(self) -> None:
        github = FakeGitHub(default="master").configured(("master", "branch"))
        status, _output, errors = self.run_program(github, "--branch", "master",
                                                   credentials=["--app-json", str(self.app_json)])
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.policies, [{"name": "master", "type": "branch"}])
        self.assertNotIn(f"api -X POST {POLICIES}", [" ".join(a[:4]) for a in github.writes()])
        self.assertEqual(github.secrets, {"MOD_BASE_GATE_APP_PRIVATE_KEY": KEY})
        self.assert_key_only_on_stdin(github)

    def test_refusals_happen_before_any_write(self) -> None:
        cases = {
            "another branch": (FakeGitHub().configured(("main", "branch"), ("release/*", "branch")), (),
                               "also admits release/* (branch)"),
            "a tag": (FakeGitHub().configured(("v*", "tag")), (), "also admits v* (tag)"),
            "reviewers": (FakeGitHub().configured(("main", "branch"), rules=("required_reviewers",)), (),
                          "has required_reviewers"),
            "wait timer": (FakeGitHub().configured(rules=("wait_timer",)), (), "has wait_timer"),
            "not the default branch": (FakeGitHub(), ("--branch", "develop"), "is not the default branch main"),
        }
        repository_secret, organization_secret = FakeGitHub(), FakeGitHub()
        repository_secret.repo_secrets = ["MOD_BASE_GATE_APP_PRIVATE_KEY"]
        organization_secret.org_secrets = ["OTHER", "MOD_BASE_GATE_APP_PRIVATE_KEY"]
        cases["repository secret"] = (repository_secret, (), "as a repository secret")
        cases["organization secret"] = (organization_secret, (), "as an organization secret")
        for name, (github, extra, message) in cases.items():
            with self.subTest(name):
                status, _output, errors = self.run_program(github, *extra)
                self.assertEqual(status, 2)
                self.assertIn(message, errors)
                self.assertEqual(github.writes(), [])
                self.assertEqual((github.variables, github.secrets), ({}, {}))

    def test_an_environment_that_does_not_read_back_restricted_gets_no_credentials(self) -> None:
        github = FakeGitHub()
        github.ignore_put = True
        status, _output, errors = self.run_program(github)
        self.assertEqual(status, 2)
        self.assertIn("does not exist after it was created", errors)
        github = FakeGitHub()
        github.environment = {"name": "mod-base-gate", "deployment_branch_policy": None, "protection_rules": []}
        github.ignore_put = True
        status, _output, errors = self.run_program(github)
        self.assertEqual(status, 2)
        self.assertIn("does not restrict its deployment branches", errors)
        self.assertEqual((github.variables, github.secrets), ({}, {}))

    def test_an_environment_open_to_every_branch_is_narrowed_first(self) -> None:
        github = FakeGitHub()
        github.environment = {"name": "mod-base-gate", "deployment_branch_policy": None, "protection_rules": []}
        status, _output, errors = self.run_program(github)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.policies, [{"name": "main", "type": "branch"}])
        self.assert_key_only_on_stdin(github)

    def test_dry_run_only_reads(self) -> None:
        github = FakeGitHub()
        status, output, errors = self.run_program(github, "--dry-run")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.writes(), [])
        self.assertTrue(all(stdin in (None, b"") for _arguments, stdin in github.calls))
        self.assertIn("would create", output)

    def test_a_gh_failure_stops_the_run_without_the_key(self) -> None:
        github = FakeGitHub()
        github.fail = "secret set"
        status, _output, errors = self.run_program(github)
        self.assertEqual(status, 2)
        self.assertIn("gh secret set MOD_BASE_GATE_APP_PRIVATE_KEY --repo failed (1): HTTP 502", errors)

    def test_credentials_are_checked_without_echoing_them(self) -> None:
        not_a_key = private_file(self.root / "notes.txt", b"client secret, not a key\n")
        oversized = private_file(self.root / "big.pem", KEY[:32] + b"A" * (new_mod_env.MAX_KEY_BYTES + 1))
        broken_json = private_file(self.root / "broken.json", ('{"client_id": "' + CLIENT + '", "pem": ').encode())
        cases = {
            "not a PEM": (["--client-id", CLIENT, "--key-file", str(not_a_key)], "is not a PEM private key"),
            "oversized": (["--client-id", CLIENT, "--key-file", str(oversized)], "is larger than"),
            "missing": (["--client-id", CLIENT, "--key-file", str(self.root / "absent.pem")], "does not exist"),
            # POSIX opens a directory and refuses its type; Windows refuses to open it.
            "directory": (["--client-id", CLIENT, "--key-file", str(self.root)],
                          r"is not a regular file or a pipe|cannot be opened"),
            "bad client": (["--client-id", "12345", "--key-file", str(self.key_file)], "not a GitHub App client ID"),
            "no key": (["--client-id", CLIENT], "give --key-file"),
            "both forms": (["--app-json", str(self.app_json), "--key-file", str(self.key_file)], "not both"),
            "broken JSON": (["--app-json", str(broken_json)], "is not JSON"),
        }
        for name, (credentials, message) in cases.items():
            with self.subTest(name):
                github = FakeGitHub()
                status, _output, errors = self.run_program(github, credentials=credentials)
                self.assertEqual(status, 2)
                self.assertRegex(errors, message)
                self.assertEqual(github.calls, [])

    def test_a_credential_file_other_users_can_read_is_refused_before_any_call(self) -> None:
        for name, credentials in (("key file", ["--key-file", str(self.key_file)]),
                                  ("manifest JSON", ["--app-json", str(self.app_json)])):
            with self.subTest(name):
                share_with_everyone(Path(credentials[1]))
                github = FakeGitHub()
                status, _output, errors = self.run_program(github, credentials=credentials)
                self.assertEqual(status, 2)
                self.assertRegex(errors, r"has mode 0644, open to other users|is readable by Everyone")
                self.assertEqual(github.calls, [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_a_group_readable_key_file_is_refused(self) -> None:
        self.key_file.chmod(0o640)
        github = FakeGitHub()
        status, _output, errors = self.run_program(github)
        self.assertEqual(status, 2)
        self.assertIn("has mode 0640, open to other users", errors)
        self.assertEqual(github.calls, [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "process substitution paths are /dev/fd/N on Linux")
    def test_a_pipe_from_a_password_manager_is_accepted(self) -> None:
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        writer = threading.Thread(target=lambda: (os.write(write_end, KEY), os.close(write_end)))
        writer.start()
        github = FakeGitHub()
        status, _output, errors = self.run_program(github, credentials=["--key-file", f"/dev/fd/{read_end}"])
        writer.join()
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.secrets, {"MOD_BASE_GATE_APP_PRIVATE_KEY": KEY})
        self.assert_key_only_on_stdin(github)

    def test_a_key_piped_on_the_standard_input_is_accepted(self) -> None:
        program = ("import importlib.util, sys; from pathlib import Path\n"
                   f"spec = importlib.util.spec_from_file_location('m', {str(PROGRAM)!r})\n"
                   "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                   "client, key = m.load_credentials(None, None, Path('-'))\n"
                   "print(client, len(key), key.startswith(b'-----BEGIN'))\n")
        # -B: no tools/__pycache__, which the kit tree digest refuses.
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", program], input=KEY, capture_output=True,
                                   check=False, timeout=60)
        self.assertEqual((completed.returncode, completed.stderr), (0, b""))
        self.assertEqual(completed.stdout.decode().split(), ["None", str(len(KEY)), "True"])

    def test_the_client_id_is_read_from_the_gate_app_when_not_given(self) -> None:
        github = FakeGitHub()
        status, output, errors = self.run_program(github, credentials=["--key-file", str(self.key_file)])
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(github.calls[0][0], ["api", "apps/plum-mod-base-gate"])
        self.assertEqual(github.variables, {"MOD_BASE_GATE_APP_CLIENT_ID": CLIENT})
        self.assertIn(f"variable MOD_BASE_GATE_APP_CLIENT_ID: {CLIENT}", output)
        for app, message in (({"id": 1, "client_id": CLIENT}, "is not App 5256389"),
                             ({"id": 5256389, "client_id": "12345"}, "has no GitHub App client ID")):
            with self.subTest(app=app):
                github = FakeGitHub()
                github.app = app
                status, _output, errors = self.run_program(github, credentials=["--key-file", str(self.key_file)])
                self.assertEqual(status, 2)
                self.assertIn(message, errors)
                self.assertEqual(github.writes(), [])

    def test_a_gh_diagnostic_that_quotes_the_key_is_redacted(self) -> None:
        github = FakeGitHub()
        github.fail = "secret set"
        github.fail_detail = b"could not parse " + KEY_LINE.encode()
        status, _output, errors = self.run_program(github)
        self.assertEqual(status, 2)
        self.assertIn("could not parse [redacted]", errors)

    def test_the_names_are_those_of_the_managed_status_caller(self) -> None:
        caller = (KIT / "template/managed/.github/workflows/mod-base-gate-status.yml").read_text(encoding="utf-8")
        self.assertIn(f"environment: {new_mod_env.ENVIRONMENT}\n", caller)
        self.assertIn(f"client-id: ${{{{ vars.{new_mod_env.CLIENT_ID_VARIABLE} }}}}\n", caller)
        self.assertIn(f"private-key: ${{{{ secrets.{new_mod_env.PRIVATE_KEY_SECRET} }}}}\n", caller)


class NewModGuideTests(unittest.TestCase):
    GUIDE = KIT / "docs/NEW-MOD.md"

    def test_the_guide_is_linked_and_names_the_program_and_the_callers_names(self) -> None:
        guide = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("](docs/NEW-MOD.md)", (KIT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("](NEW-MOD.md)", (KIT / "docs/OPERATIONS.md").read_text(encoding="utf-8"))
        for name in ("tools/new_mod_env.py", new_mod_env.ENVIRONMENT, new_mod_env.CLIENT_ID_VARIABLE,
                     new_mod_env.PRIVATE_KEY_SECRET, new_mod_env.APP_SLUG, str(new_mod_env.APP_ID)):
            self.assertIn(name, guide)

    def test_the_guide_keeps_the_key_in_a_password_manager_only(self) -> None:
        guide = " ".join(self.GUIDE.read_text(encoding="utf-8").split())
        rule = ("generate a new private key in the App settings and keep it in a password manager, "
                "never in the home folder unencrypted")
        self.assertTrue(rule in guide, f"docs/NEW-MOD.md must say: {rule}")
        for forbidden in (".mod-base-secrets", "--app-json ~", "--key-file ~"):
            self.assertFalse(forbidden in guide, f"docs/NEW-MOD.md names {forbidden}")

    def test_every_relative_link_of_the_guide_resolves(self) -> None:
        guide = self.GUIDE.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", guide):
            if not target.startswith("https://"):
                with self.subTest(target):
                    self.assertTrue((self.GUIDE.parent / target).exists(), target)

    def test_the_template_verbs_the_guide_runs_exist(self) -> None:
        from mod_base import cli

        guide = self.GUIDE.read_text(encoding="utf-8")
        verbs = set(re.findall(r"\bmod_base(?:_kit\.py run)? template ([a-z]+)\b", guide))
        self.assertTrue({"init", "check", "sync", "activation", "transition"} <= verbs, verbs)
        for verb in sorted(verbs):
            with self.subTest(verb):
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(io.StringIO()):
                    cli.build_parser("template").parse_args(["template", verb, "--help"])
                self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
