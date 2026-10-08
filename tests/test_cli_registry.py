"""The frozen CLI surface (SPEC §2.2) and the registry's clean failure modes."""

from __future__ import annotations

import argparse
import importlib
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import mod_base
from mod_base import cli
from mod_base.errors import MbError

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
REPO = ["--repo", "mod", "--config", "mod/site/mod-base.json"]

#: command -> (argv, expected parsed attributes)
SURFACE: dict[str, tuple[list[str], dict[str, object]]] = {
    "pin verify": (["pin", "verify", "--repo", "mod", "--network"], {"network": True}),
    "digest": (["digest", "--root", "kit", "--check", DIGEST], {"check": DIGEST}),
    "template check": (["template", "check", "--repo", "mod"], {"template_command": "check"}),
    "template sync": (["template", "sync", "--repo", "mod", "--write"], {"write": True}),
    "template init": (["template", "init", "--repo", "mod", "--seed", "--from-config", "c.json"],
                      {"seed": True, "from_config": Path("c.json")}),
    "template activation": (["template", "activation", "--repo", "mod"], {"template_command": "activation"}),
    "template transition": (["template", "transition", "--repo", "candidate", "--base", "mod"],
                            {"template_command": "transition", "base": Path("mod")}),
    "expect": (["expect", *REPO, "--key", "mc1.20.1", "--tested-run-json", "t.json", "--extensions", "e.json",
                "--output", "x.json"], {"key": "mc1.20.1", "tested_run_json": Path("t.json")}),
    "prepare": ([
        "prepare", *REPO, "--e2e-root", "e2e-out", "--key", "mc1.20.1", "--output", "handoff",
        "--subject-branch", "master", "--subject-commit", SHA, "--subject-tree", SHA,
        "--tested-run-id", "12", "--tested-run-attempt", "1", "--tested-branch", "master", "--tested-commit", SHA,
        "--tested-controller-branch", "master", "--tested-controller-sha", SHA, "--extensions", "e.json",
        "--anchor", "auto", "--anchor-output", "anchor",
    ], {"tested_run_id": 12, "anchor": "auto", "anchor_output": Path("anchor")}),
    "anchor identity": (["anchor", "identity", *REPO, "--key", "mc1.20.1", "--handoff", "h"], {"handoff": Path("h")}),
    "anchor create": (["anchor", "create", *REPO, "--key", "mc1.20.1", "--handoff", "h", "--raw-artifact-id", "7",
                       "--raw-artifact-name", "mb-handoff--mc1.20.1--a1", "--raw-artifact-digest", DIGEST,
                       "--output", "a"], {"raw_artifact_id": 7}),
    "anchor validate": (["anchor", "validate", "--key", "mc1.20.1", "--input", "a", "--expected-subject-commit", SHA],
                        {"expected_subject_commit": SHA}),
    "family envelope": (["family", "envelope", *REPO, "--family", "mod-compatibility", "--key", "mc1.20.1",
                         "--bundle", "b", "--coverage-sha", SHA, "--subject-branch", "master", "--subject-commit", SHA,
                         "--output", "o"], {"family": "mod-compatibility"}),
    "family collect": (["family", "collect", *REPO, "--family", "mod-compatibility", "--key", "mc1.20.1", "--input", "i",
                        "--expected-coverage-sha", SHA, "--selected-json", "s.json", "--output", "o"],
                       {"expected_coverage_sha": SHA, "selected_json": Path("s.json")}),
    "admit": (["admit", *REPO, "--operation", "family", "--run-id", "5", "--sha", SHA, "--family", "mod-compatibility",
               "--bundle-key", "mc1.20.1", "--artifact-id", "9", "--artifact-digest", DIGEST, "--coverage-sha", SHA,
               "--github-output", "out"], {"operation": "family", "artifact_id": 9}),
    "select": (["select", *REPO, "--key", "mc1.20.1", "--family", "mod-compatibility", "--nomination", "44",
                "--expected-subject-commit", SHA, "--github-output", "out", "--output", "selected.json"],
               {"nomination": 44, "output": Path("selected.json")}),
    "download": (["download", "--artifact-id", "1", "--name", "mb-handoff--mc1.20.1--a1", "--digest", DIGEST,
                  "--size", "10", "--run-id", "2", "--output", "d"], {"size": 10}),
    "authenticate": (["authenticate", *REPO, "--key", "mc1.20.1", "--selected", "d", "--selected-json", "s.json",
                      "--output", "selection.json"], {"selected_json": Path("s.json")}),
    "compose": (["compose", *REPO, "--key", "mc1.20.1", "--selected", "d", "--selection", "s.json", "--output", "o"],
                {"selected": Path("d"), "selection": Path("s.json")}),
    "compact": (["compact", *REPO, "--key", "mc1.20.1", "--input", "i", "--selection", "s.json", "--output", "o"],
                {"selection": Path("s.json")}),
    "validate": (["validate", *REPO, "--key", "mc1.20.1", "--kind", "compact", "--input", "i", "--bind-raw", "r",
                  "--expected-subject-commit", SHA], {"kind": "compact", "bind_raw": Path("r")}),
    "build": (["build", *REPO, "--kit-root", "kit", "--collected", "c", "--families", "f", "--output", "_site",
               "--promotion", "p", "--github-output", "out"], {"output": Path("_site")}),
    "refresh": (["refresh", *REPO, "--key", "mc1.20.1", "--family", "mod-compatibility", "--input", "i",
                 "--github-output", "out"], {"family": "mod-compatibility"}),
    "rotate": (["rotate", *REPO, "--owner-run-id", "3", "--owner-sha", SHA, "--delete-delay-seconds", "1.0",
                "--dry-run"], {"owner_run_id": 3, "dry_run": True, "delete_delay_seconds": 1.0}),
    "conformance": (["conformance", "--repo", "mod", "--keys", "mc1.20.1,mc26.3", "--kit-root", ".", "--families"],
                    {"keys": ("mc1.20.1", "mc26.3"), "families": True}),
    "conformance all": (["conformance", "--repo", "mod", "--all"], {"all_keys": True, "keys": None}),
    "budget": (["budget"], {}),
    "ci subject": (["ci", "subject", *REPO, "--state", "state", "--producer", "build", "--pr", "7",
                    "--github-output", "out"],
                   {"ci_command": "subject", "state": Path("state"), "producer": "build", "pr": 7,
                    "github_output": Path("out")}),
    "ci subject protected": (["ci", "subject", *REPO, "--state", "state", "--producer", "packaged", "--pr", "",
                              "--github-output", "out"], {"producer": "packaged", "pr": None}),
    "ci worker-prepare": (["ci", "worker-prepare", *REPO, "--state", "state", "--roles", "candidate+validator",
                           "--python", "/opt/python/bin/python3", "--java-home", "/opt/jdk/17",
                           "--java-home", "/opt/jdk/21"],
                          {"ci_command": "worker-prepare", "state": Path("state"), "roles": "candidate+validator",
                           "python": "/opt/python/bin/python3", "java_home": ["/opt/jdk/17", "/opt/jdk/21"]}),
    "ci worker-prepare validator": (["ci", "worker-prepare", *REPO, "--state", "state", "--roles", "validator",
                                     "--python", "/opt/python/bin/python3"], {"roles": "validator", "java_home": []}),
    "ci plan": (["ci", "plan", *REPO, "--state", "state"],
                {"ci_command": "plan", "state": Path("state"), "candidate": None, "expect_sha256": None,
                 "github_output": None}),
    "ci plan candidate": (["ci", "plan", *REPO, "--state", "state", "--candidate", "candidate",
                           "--expect-sha256", "ab" * 32, "--github-output", "out"],
                          {"candidate": Path("candidate"), "expect_sha256": "ab" * 32, "github_output": Path("out")}),
    "ci worker-finish": (["ci", "worker-finish", *REPO, "--state", "state"],
                         {"ci_command": "worker-finish", "state": Path("state")}),
    "ci batch-prepare": (["ci", "batch-prepare", *REPO, "--state", "state", "--name", "run-1", "--allowed-paths",
                          "allowed.json", "--dry-run", "--github-output", "out", "12", "7"],
                         {"ci_command": "batch-prepare", "state": Path("state"), "name": "run-1",
                          "allowed_paths": Path("allowed.json"), "dry_run": True, "github_output": Path("out"),
                          "pulls": [12, 7]}),
    "ci batch-settle": (["ci", "batch-settle", *REPO, "--state", "state", "--pr", "9", "--plan", "plan.json",
                         "--build-seal", "build.json", "--packaged-seal", "packaged.json", "--delete-branches"],
                        {"ci_command": "batch-settle", "state": Path("state"), "pr": 9, "plan": Path("plan.json"),
                         "build_seal": Path("build.json"), "packaged_seal": Path("packaged.json"),
                         "delete_branches": True, "github_output": None}),
    "ci select-build": (["ci", "select-build", *REPO, "--state", "state", "--output", "selection.json",
                         "--github-output", "out"],
                        {"ci_command": "select-build", "state": Path("state"), "wait_seconds": 5400,
                         "build_run_id": None, "output": Path("selection.json"), "github_output": Path("out")}),
    "ci select-build pull request": (["ci", "select-build", *REPO, "--state", "state", "--wait-seconds", "600",
                                      "--build-run-id", "", "--output", "s.json", "--github-output", "out"],
                                     {"wait_seconds": 600, "build_run_id": None}),
    "ci select-build named": (["ci", "select-build", *REPO, "--state", "state", "--build-run-id", "42",
                               "--output", "s.json", "--github-output", "out"], {"build_run_id": 42}),
    "ci select-build same run": (["ci", "select-build", *REPO, "--state", "state", "--build-run-id", "same-run",
                                  "--output", "s.json", "--github-output", "out"], {"build_run_id": "same-run"}),
    "ci fetch-build": (["ci", "fetch-build", *REPO, "--state", "state", "--selection", "selection.json"],
                       {"ci_command": "fetch-build", "state": Path("state"), "selection": Path("selection.json")}),
    "ci gate-status": (["ci", "gate-status", *REPO, "--state", "state", "--pr", "7", "--github-output", "out"],
                       {"ci_command": "gate-status", "state": Path("state"), "pr": 7, "github_output": Path("out")}),
}


def parse(argv: list[str]):
    return cli.build_parser(argv[0]).parse_args(argv)


def run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class SurfaceTest(unittest.TestCase):
    def test_registry_covers_every_spec_command(self) -> None:
        self.assertEqual(set(cli.COMMANDS), {
            "pin", "digest", "template", "expect", "prepare", "anchor", "family", "admit", "select", "download",
            "authenticate", "compose", "compact", "validate", "build", "refresh", "rotate", "conformance", "budget",
            "ci",
        })
        self.assertEqual(cli.COMMANDS["ci"], "mod_base.build_ci.commands")
        self.assertEqual(cli.COMMANDS["download"], "mod_base.github.commands")
        self.assertEqual(cli.COMMANDS["admit"], "mod_base.pages.commands_control")
        self.assertEqual(cli.COMMANDS["build"], "mod_base.pages.commands_build")
        self.assertEqual(cli.COMMANDS["rotate"], "mod_base.pages.commands_rotate")
        self.assertEqual(cli.COMMANDS["digest"], "mod_base.pin_commands")
        self.assertEqual({cli.COMMANDS[name] for name in ("expect", "prepare", "validate", "compact", "compose", "anchor")},
                         {"mod_base.evidence.commands"})
        seen = {argv[0] for argv, _ in SURFACE.values()}
        self.assertEqual(seen, set(cli.COMMANDS))

    def test_every_command_parses_its_frozen_flags(self) -> None:
        for label, (argv, expected) in SURFACE.items():
            with self.subTest(command=label):
                namespace = parse(argv)
                self.assertTrue(callable(namespace.handler))
                for name, value in expected.items():
                    self.assertEqual(getattr(namespace, name), value)

    def test_config_defaults_to_none(self) -> None:
        namespace = parse(["compact", "--repo", "mod", "--key", "k1", "--input", "i", "--selection", "s", "--output", "o"])
        self.assertIsNone(namespace.config)

    def test_malformed_values_are_rejected_at_parse_time(self) -> None:
        bad = [
            ["prepare", *REPO, "--e2e-root", "e", "--key", "MC", "--output", "o"],
            ["select", *REPO, "--key", "k1", "--expected-subject-commit", "abc", "--github-output", "o"],
            ["download", "--artifact-id", "0", "--name", "n", "--digest", DIGEST, "--size", "1", "--run-id", "1", "--output", "o"],
            ["download", "--artifact-id", "1", "--name", "n", "--digest", "b" * 64, "--size", "1", "--run-id", "1", "--output", "o"],
            ["admit", *REPO, "--operation", "rotate", "--github-output", "o"],
            ["validate", *REPO, "--key", "k1", "--kind", "raw", "--input", "i"],
            ["rotate", *REPO, "--owner-run-id", "3", "--owner-sha", SHA, "--delete-delay-seconds", "nan"],
            ["conformance", "--repo", "m", "--keys", "k1,k1"],
            ["conformance", "--repo", "m", "--keys", "k1", "--all"],
            ["family", "collect", *REPO, "--family", "Bad", "--key", "k1", "--input", "i",
             "--expected-coverage-sha", SHA, "--selected-json", "s.json", "--output", "o"],
            # the recorded selection is required (the MB0 --selected-json amendment)
            ["family", "collect", *REPO, "--family", "mod-compatibility", "--key", "k1", "--input", "i",
             "--expected-coverage-sha", SHA, "--output", "o"],
            ["prepare", "--repo", "m"],
            ["compose", *REPO, "--key", "k1", "--selected", "d", "--output", "o"],
            ["ci"],
            ["ci", "deploy"],
            ["ci", "subject", *REPO, "--state", "s", "--producer", "status", "--pr", "7", "--github-output", "o"],
            ["ci", "subject", *REPO, "--state", "s", "--producer", "build", "--pr", "0", "--github-output", "o"],
            ["ci", "subject", *REPO, "--state", "s", "--producer", "build", "--pr", "seven", "--github-output", "o"],
            ["ci", "subject", *REPO, "--state", "s", "--producer", "build", "--github-output", "o"],
            ["ci", "subject", *REPO, "--producer", "build", "--pr", "7", "--github-output", "o"],
            ["ci", "subject", *REPO, "--state", "s", "--producer", "build", "--pr", "7"],
            ["ci", "subject", "--state", "s", "--producer", "build", "--pr", "7", "--github-output", "o"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--python", "/opt/python/bin/python3"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--roles", "validator"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--roles", "candidate", "--python", "/opt/python/bin/python3"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--roles", "validator", "--python", "python3"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--roles", "validator", "--python", "/opt/x/../python3"],
            ["ci", "worker-prepare", *REPO, "--state", "s", "--roles", "validator", "--python", "/opt/python/bin/python3",
             "--java-home", "jdk"],
            ["ci", "worker-prepare", *REPO, "--roles", "validator", "--python", "/opt/python/bin/python3"],
            ["ci", "plan", *REPO],
            ["ci", "plan", *REPO, "--state", "s", "--expect-sha256", "AB" * 32],
            ["ci", "plan", *REPO, "--state", "s", "--expect-sha256", "ab" * 31],
            ["ci", "plan", *REPO, "--state", "s", "--candidate", ""],
            ["ci", "plan", *REPO, "--state", "s", "--roles", "validator"],
            ["ci", "worker-finish", *REPO],
            ["ci", "worker-finish", "--state", "s"],
            ["ci", "worker-finish", *REPO, "--state", "s", "--python", "/opt/python/bin/python3"],
            ["ci", "batch-prepare", *REPO, "--state", "s", "--name", "run-1", "--allowed-paths", "a.json"],
            ["ci", "batch-prepare", *REPO, "--state", "s", "--name", "Run/1", "--allowed-paths", "a.json", "7"],
            ["ci", "batch-prepare", *REPO, "--name", "run-1", "--allowed-paths", "a.json", "7"],
            ["ci", "batch-settle", *REPO, "--state", "s", "--pr", "0", "--plan", "p", "--build-seal", "b",
             "--packaged-seal", "e"],
            ["ci", "batch-settle", *REPO, "--state", "s", "--pr", "9", "--plan", "p", "--build-seal", "b"],
            ["ci", "select-build", *REPO, "--state", "s", "--output", "o"],
            ["ci", "select-build", *REPO, "--state", "s", "--github-output", "o"],
            ["ci", "select-build", *REPO, "--output", "o", "--github-output", "o"],
            ["ci", "select-build", *REPO, "--state", "s", "--build-run-id", "latest", "--output", "o",
             "--github-output", "o"],
            ["ci", "select-build", *REPO, "--state", "s", "--build-run-id", "0", "--output", "o",
             "--github-output", "o"],
            ["ci", "select-build", *REPO, "--state", "s", "--wait-seconds", "5401", "--output", "o",
             "--github-output", "o"],
            ["ci", "select-build", *REPO, "--state", "s", "--wait-seconds", "0", "--output", "o",
             "--github-output", "o"],
            ["ci", "fetch-build", *REPO, "--state", "s"],
            ["ci", "fetch-build", *REPO, "--selection", "f"],
            ["ci", "gate-status", *REPO, "--state", "s", "--pr", "0", "--github-output", "o"],
            ["ci", "gate-status", *REPO, "--state", "s", "--pr", "", "--github-output", "o"],
            ["ci", "gate-status", *REPO, "--state", "s", "--github-output", "o"],
            ["ci", "gate-status", *REPO, "--state", "s", "--pr", "7"],
            ["ci", "gate-status", *REPO, "--pr", "7", "--github-output", "o"],
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(MbError) as caught:
                parse(argv)
            self.assertEqual(caught.exception.exit_code, 2)
            self.assertEqual(caught.exception.reason, "usage")

    def test_usage_errors_are_one_line_exit_2(self) -> None:
        code, stdout, stderr = run(["select", "--repo", "m", "--key", "Bad key"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count("\n"), 1)
        self.assertTrue(stderr.startswith("mod_base: usage: "))

    def test_help_version_and_missing_command(self) -> None:
        code, stdout, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("conformance", stdout)
        code, stdout, _ = run(["--version"])
        self.assertEqual((code, stdout), (0, f"mod-base {mod_base.__version__}\n"))
        code, _, stderr = run([])
        self.assertEqual(code, 2)
        self.assertIn("a command is required", stderr)
        code, _, stderr = run(["deploy"])
        self.assertEqual(code, 2)
        self.assertIn("unknown command 'deploy'", stderr)


class CiVerbsTest(unittest.TestCase):
    """``ci`` gathers its verbs from the modules ``build_ci.commands.VERB_MODULES`` lists."""

    @staticmethod
    def verbs() -> dict[str, argparse.ArgumentParser]:
        def choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
            (action,) = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
            return dict(action.choices)

        return choices(choices(cli.build_parser("ci"))["ci"])

    def test_every_listed_module_adds_verbs_that_take_the_job_arguments(self) -> None:
        from mod_base.build_ci import commands

        self.assertLessEqual({"mod_base.build_ci.commands_subject", "mod_base.build_ci.commands_worker",
                              "mod_base.build_ci.commands_batch", "mod_base.build_ci.commands_packaged",
                              "mod_base.build_ci.commands_status"}, set(commands.VERB_MODULES))
        self.assertEqual(len(set(commands.VERB_MODULES)), len(commands.VERB_MODULES))
        for name in commands.VERB_MODULES:
            with self.subTest(module=name):
                self.assertTrue(callable(importlib.import_module(name).add_verbs))
        verbs = self.verbs()
        self.assertLessEqual({"subject", "worker-prepare", "plan", "worker-finish", "batch-prepare",
                              "batch-settle", "select-build", "fetch-build", "gate-status"}, set(verbs))
        for name, parser in verbs.items():
            options = {option for action in parser._actions for option in action.option_strings}
            with self.subTest(verb=name):
                self.assertTrue(callable(parser.get_default("handler")))
                self.assertLessEqual({"--repo", "--config", "--state"}, options)
                self.assertIs(type(parser), cli.KitArgumentParser)

    def test_the_verbs_of_a_module_come_from_its_add_verbs(self) -> None:
        from mod_base.build_ci import commands

        module = types.ModuleType("fake_verbs")

        def add_verbs(verbs) -> None:
            parser = verbs.add_parser("probe")
            commands.add_job_arguments(parser)
            parser.set_defaults(handler=lambda args: 0)

        module.add_verbs = add_verbs  # type: ignore[attr-defined]
        # Exactly this one name is registered and removed. mock.patch.dict(sys.modules, ...) would put
        # the whole table back, and so also forget every verb module `ci` first imports in here while
        # the mod_base.build_ci package keeps each one as an attribute. Whatever this process imports
        # next then mixes two copies of them: `from mod_base.build_ci import batch` is the forgotten
        # copy, `from mod_base.build_ci.batch_git import ...` executes a second one.
        sys.modules["fake_verbs"] = module
        self.addCleanup(sys.modules.pop, "fake_verbs", None)
        with mock.patch.object(commands, "VERB_MODULES", (*commands.VERB_MODULES, "fake_verbs")):
            self.assertLessEqual({"subject", "probe"}, set(self.verbs()))
            namespace = parse(["ci", "probe", "--repo", "mod", "--state", "state"])
            self.assertEqual((namespace.ci_command, namespace.state, namespace.config), ("probe", Path("state"), None))
            self.assertEqual(run(["ci", "probe", "--repo", "mod"])[0], 2)


class UnavailableGroupTest(unittest.TestCase):
    """A missing or unimplemented group fails with one MbError line, never an import traceback."""

    def run_with(self, command: str, module_name: str, module: types.ModuleType | None) -> tuple[int, str]:
        modules = dict(sys.modules)
        if module is not None:
            modules[module_name] = module
        with mock.patch.dict(cli.COMMANDS, {command: module_name}), mock.patch.dict(sys.modules, modules, clear=True):
            code, _, stderr = run([command])
        return code, stderr

    def test_missing_module(self) -> None:
        code, stderr = self.run_with("budget", "mod_base.not_a_real_module", None)
        self.assertEqual(code, 2)
        self.assertIn("command 'budget' is unavailable: mod_base.not_a_real_module cannot be imported", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stderr.count("\n"), 1)

    def test_module_without_register(self) -> None:
        code, stderr = self.run_with("budget", "fake_group", types.ModuleType("fake_group"))
        self.assertEqual(code, 2)
        self.assertIn("defines no register()", stderr)

    def test_module_that_does_not_register_the_command(self) -> None:
        module = types.ModuleType("fake_group")
        module.register = lambda subparsers: subparsers.add_parser("other")  # type: ignore[attr-defined]
        code, stderr = self.run_with("budget", "fake_group", module)
        self.assertEqual(code, 2)
        self.assertIn("does not register it", stderr)

    def test_unimplemented_entry_point(self) -> None:
        module = types.ModuleType("fake_group")

        def handler(args: object) -> int:
            raise NotImplementedError("owned by MB99")

        def register(subparsers) -> None:
            subparsers.add_parser("budget").set_defaults(handler=handler)

        module.register = register  # type: ignore[attr-defined]
        code, stderr = self.run_with("budget", "fake_group", module)
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "mod_base: not implemented: owned by MB99\n")

    def test_import_error_inside_a_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken_group.py"
            path.write_text("import definitely_missing_dependency\n", encoding="utf-8")
            sys.path.insert(0, directory)
            try:
                with mock.patch.dict(cli.COMMANDS, {"budget": "broken_group"}):
                    code, _, stderr = run(["budget"])
            finally:
                sys.path.remove(directory)
                sys.modules.pop("broken_group", None)
        self.assertEqual(code, 2)
        self.assertIn("broken_group cannot be imported (ModuleNotFoundError", stderr)
        self.assertNotIn("Traceback", stderr)


class SelectOutputTest(unittest.TestCase):
    """``select --output F`` writes the frozen Selected JSON object for ``authenticate``."""

    def test_writes_a_new_file_and_refuses_an_existing_one(self) -> None:
        from mod_base.pages import commands_control, select

        chosen = select.Selected(kind="handoff", artifact_id=9, name="mb-handoff--mc1.20.1--a1", digest=DIGEST,
                                 size=10, run_id=2, run_attempt=1)
        document = {name: getattr(chosen, name) for name in select.SELECTED_KEYS}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(commands_control.runtime, "build_invocation", return_value=mock.sentinel.invocation), \
                mock.patch.object(commands_control, "_client", return_value=mock.sentinel.api), \
                mock.patch.object(select, "select_evidence", return_value=chosen), \
                mock.patch.object(select.Selected, "to_json", lambda self: document):
            output = Path(directory) / "selected.json"
            argv = ["select", *REPO, "--key", "mc1.20.1", "--expected-subject-commit", SHA,
                    "--github-output", str(Path(directory) / "out"), "--output", str(output)]
            self.assertEqual(run(argv)[0], 0)
            from mod_base.model.canonical import canonical_json

            self.assertEqual(output.read_bytes(), canonical_json(document))
            code, _, stderr = run(argv)
            self.assertEqual(code, 2)
            self.assertIn("cannot create", stderr)

    def test_selected_keys_are_the_dataclass_fields(self) -> None:
        import dataclasses

        from mod_base.pages import select

        self.assertEqual(select.SELECTED_KEYS, tuple(field.name for field in dataclasses.fields(select.Selected)))


class GithubOutputTest(unittest.TestCase):
    def test_appends_single_line_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            path.write_text("existing=1\n", encoding="utf-8")
            cli.write_github_output(path, {"eligible": True, "count": 3, "reason": "current", "empty": ""})
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "existing=1\neligible=true\ncount=3\nreason=current\nempty=\n")
            cli.write_github_output(None, {"ignored": "x"})

    def test_rejects_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            for values in ({"a": "x\nb=y"}, {"a": "x\r"}, {"Bad": "x"}, {"a-b": "x"}, {"a": 1.5}, {"a": None}):
                with self.subTest(values=values), self.assertRaises(MbError):
                    cli.write_github_output(path, values)  # type: ignore[arg-type]
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
