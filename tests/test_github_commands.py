"""``mod_base download`` and ``mod_base budget`` (SPEC §2.2): the CLI wiring of the verified
artifact download and of Quick Skin's ``github_api_budget_snapshot`` equivalent."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mod_base import cli
from mod_base.errors import MbError
from mod_base.github import commands
from mod_base.github.api import ApiError
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from tests.helpers import REPOSITORY
from tests.test_github_artifacts import RUN_ID, record
from tests.test_io_bounded_zip import archive

NAME = grammar.handoff_name("mc26.3", 2)


class CommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.api = FakeGitHub(repository=REPOSITORY)
        self.environments: list[Any] = []

    def run_cli(self, argv: list[str], *, api: Any = None, error: BaseException | None = None) -> tuple[int, str, str]:
        def factory(environ: Any, **options: Any) -> Any:
            self.environments.append((environ, options))
            if error is not None:
                raise error
            return self.api if api is None else api

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(commands.github_api, "from_environment", factory), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class DownloadCommandTests(CommandTestCase):
    def arguments(self, data: bytes, **changes: str) -> list[str]:
        values = {"--artifact-id": "7", "--name": NAME, "--digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                  "--size": str(len(data)), "--run-id": str(RUN_ID), "--output": str(self.root / "out"), **changes}
        return ["download", *(item for pair in values.items() for item in pair)]

    def test_downloads_and_extracts_with_the_limits_of_the_artifact_kind(self) -> None:
        data = archive({"manifest.json": b"{}", "runtime/frame.png": b"\x89PNG"})
        self.api.add_artifact(record(7, NAME, data), data)
        code, stdout, stderr = self.run_cli(self.arguments(data))
        self.assertEqual((0, "", ""), (code, stdout, stderr))
        self.assertEqual(b"{}", (self.root / "out" / "manifest.json").read_bytes())
        self.assertEqual([{}], [options for _, options in self.environments])  # read-only, no write scope

    def test_the_kind_limits_reject_what_the_kind_never_contains(self) -> None:
        data = archive({"manifest.json": b"{}", "runtime/tool.sh": b"#!/bin/sh"})
        self.api.add_artifact(record(7, NAME, data), data)
        code, _, stderr = self.run_cli(self.arguments(data))
        self.assertEqual(2, code)
        self.assertIn("unapproved suffix", stderr)
        self.assertFalse((self.root / "out").exists())

    def test_foreign_names_and_malformed_flags_are_usage_errors_before_any_request(self) -> None:
        data = archive({"manifest.json": b"{}"})
        for changes in ({"--name": "pages-cache-master"}, {"--name": "github-pages"}, {"--digest": "sha256:abc"},
                        {"--size": "0"}, {"--artifact-id": "-1"}, {"--run-id": "x"}):
            with self.subTest(changes=changes):
                code, _, stderr = self.run_cli(self.arguments(data, **changes))
                self.assertEqual(2, code)
                self.assertEqual(1, len(stderr.splitlines()))
        self.assertEqual([], self.environments)
        self.assertEqual(0, self.api.request_count)

    def test_selection_mismatch_is_a_rejection(self) -> None:
        data = archive({"manifest.json": b"{}"})
        self.api.add_artifact(record(7, NAME, data), data)
        code, _, stderr = self.run_cli(self.arguments(data, **{"--run-id": str(RUN_ID + 1)}))
        self.assertEqual(2, code)
        self.assertIn("differs from the selected artifact", stderr)


class BudgetCommandTests(CommandTestCase):
    def test_prints_only_the_numeric_core_counters_as_canonical_json(self) -> None:
        self.api.get_json(f"/repos/{REPOSITORY}")
        code, stdout, stderr = self.run_cli(["budget"])
        self.assertEqual((0, ""), (code, stderr))
        counters = json.loads(stdout)
        self.assertEqual({"limit", "used", "remaining", "reset"}, set(counters))
        self.assertTrue(all(isinstance(value, int) for value in counters.values()))
        self.assertEqual(json.dumps(counters, sort_keys=True, separators=(",", ":")) + "\n", stdout)

    def test_unavailable_telemetry_never_fails_the_step(self) -> None:
        class Unavailable(FakeGitHub):
            def rate_limit_snapshot(self) -> dict[str, int]:
                raise ApiError("GitHub API GET /rate_limit failed in transport: token=secret", status=0,
                               method="GET", path="/rate_limit")

        failing = Unavailable(repository=REPOSITORY)
        for label, options in (("api", {"api": failing}),
                               ("environment", {"error": MbError("GH_TOKEN or GITHUB_TOKEN is required",
                                                                 reason="environment")})):
            with self.subTest(label=label):
                code, stdout, stderr = self.run_cli(["budget"], **options)
                self.assertEqual((0, ""), (code, stdout))
                self.assertIn("telemetry unavailable", stderr)
                self.assertNotIn("secret", stderr)

    def test_a_malformed_api_url_is_unavailable_telemetry_too(self) -> None:
        for url in ("https://[evil]", "https://[::1", "http://api.github.com"):
            with self.subTest(url=url):
                environ = {"GITHUB_REPOSITORY": REPOSITORY, "GH_TOKEN": "fixture-token", "GITHUB_API_URL": url}
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli, "environ", return_value=environ), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = cli.main(["budget"])
                self.assertEqual((0, ""), (code, stdout.getvalue()))
                self.assertIn("telemetry unavailable (usage)", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
