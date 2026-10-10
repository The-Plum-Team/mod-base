"""``ci batch-prepare`` and ``ci batch-settle`` through the ``ci`` parser, with pinned request counts.

GitHub is replaced at its two doors only: the API client factory answers with the fake of
``tests/fixtures/ci_batch/support.py`` and the Git remote is that fixture's local repository.
The registration, the handlers, the constructor, the settlement and the Git writer run for real.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base import cli
from mod_base.build_ci import commands, commands_batch
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests.fixtures.ci_batch.support import ALLOWED, BASE_FILES, POSIX_ONLY, REPOSITORY, World
from tests.test_ci_batch_settle import GATE_PAIR_REQUESTS

CONFIG = Path(__file__).resolve().parent / "fixtures" / "mods" / "qs_like" / "site" / "mod-base.json"


def parse(*argv: str) -> argparse.Namespace:
    """``mod_base ci <argv>`` as the command line parses it."""

    return cli.build_parser("ci").parse_args(["ci", *argv])


class SurfaceTests(unittest.TestCase):
    REPO = ["--repo", "mod", "--config", "mod/site/mod-base.json", "--state", "state"]

    def test_flags(self):
        args = parse("batch-prepare", *self.REPO, "--name", "n1", "--allowed-paths", "a.json", "--dry-run",
                     "--github-output", "out", "12", "7")
        self.assertEqual((args.pulls, args.name, args.dry_run, args.allowed_paths, args.state, args.github_output),
                         ([12, 7], "n1", True, Path("a.json"), Path("state"), Path("out")))
        self.assertIs(args.handler, commands_batch.run_batch_prepare)
        settle = ["batch-settle", *self.REPO, "--pr", "9", "--plan", "p.json", "--build-seal", "b.json",
                  "--packaged-seal", "e.json"]
        args = parse(*settle, "--delete-branches")
        self.assertEqual((args.pr, args.plan, args.build_seal, args.packaged_seal, args.delete_branches,
                          args.state, args.github_output),
                         (9, Path("p.json"), Path("b.json"), Path("e.json"), True, Path("state"), None))
        self.assertIs(args.handler, commands_batch.run_batch_settle)
        for argv in (["batch-prepare", *self.REPO, "--name", "n1", "--allowed-paths", "a.json"],
                     ["batch-prepare", *self.REPO, "--name", "../x", "--allowed-paths", "a.json", "1"],
                     ["batch-prepare", *self.REPO, "--name", "n1", "--allowed-paths", "a.json", "0"],
                     ["batch-prepare", *self.REPO, "--name", "n1", "1"],
                     ["batch-prepare", "--repo", "mod", "--name", "n1", "--allowed-paths", "a.json", "1"],
                     ["batch-settle", *self.REPO, "--pr", "9"],
                     ["batch-settle", *self.REPO, "--pr", "0", *settle[-6:]],
                     # A seal names the run that sealed it: no workflow is a command-line argument.
                     [*settle, "--build-workflow", ".github/workflows/mod-base-build.yml"],
                     ["batch-verify"]):
            with self.subTest(argv=argv[-3:]), self.assertRaises(MbError) as caught:
                parse(*argv)
            self.assertEqual(caught.exception.reason, "usage")


@POSIX_ONLY
class CommandTests(unittest.TestCase):
    def world(self, budget: int) -> World:
        world = World(self, max_requests=budget)
        self.mod = world.root / "mod"
        (self.mod / "site").mkdir(parents=True)
        shutil.copyfile(CONFIG, self.mod / "site" / "mod-base.json")
        self.allowed = world.root / "allowed.json"
        self.allowed.write_bytes(canonical_json(list(ALLOWED)))
        self.output = world.root / "github-output"
        self.clients: list[tuple] = []
        self.remotes: list[tuple] = []
        return world

    def run_verb(self, world: World, *argv: str) -> dict:
        environ = {"GITHUB_REPOSITORY": REPOSITORY, "GH_TOKEN": "t0ken", "GITHUB_OUTPUT": str(self.output),
                   "GIT_DIR": "/nonexistent"}

        def client(environment, *, writable=False, max_requests=None):
            self.clients.append((environment.get("GITHUB_REPOSITORY"), writable, max_requests))
            return world.hub

        def remote(repository, token):
            self.remotes.append((repository, token))
            return world.git_remote

        stdout = io.TextIOWrapper(io.BytesIO())
        args = parse(argv[0], "--repo", str(self.mod), "--state", str(world.state), *argv[1:])
        with patch.object(commands.github_api, "from_environment", side_effect=client), \
                patch.object(commands_batch.batch_git, "github_remote", side_effect=remote), \
                patch.object(cli, "environ", return_value=environ), patch.object(sys, "stdout", stdout):
            self.assertEqual(args.handler(args), 0)
        stdout.flush()
        raw = stdout.buffer.getvalue()
        document = json.loads(raw)
        self.assertEqual(canonical_json(document), raw)
        self.assertEqual(self.remotes[-1], (REPOSITORY, "t0ken"))
        self.assertEqual(list(world.state.iterdir()), [])
        return document

    def members(self, world: World, count: int) -> list[str]:
        for number in range(1, count + 1):
            world.member(number, f"fix/n{number}", {**BASE_FILES, f"src/n{number}.txt": b"x\n"})
        return [str(number) for number in range(1, count + 1)]

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines())

    def test_a_dry_run_asks_for_a_read_only_client_and_writes_nothing(self):
        world = self.world(limits.MAX_CI_BATCH_PREPARE_REQUESTS)
        numbers = self.members(world, 3)
        refs = world.remote.refs()
        result = self.run_verb(world, "batch-prepare", "--name", "dry", "--allowed-paths", str(self.allowed),
                               "--dry-run", *reversed(numbers))
        self.assertEqual((result["dry_run"], result["pr_number"]), (True, None))
        self.assertEqual([member["pr_number"] for member in result["manifest"]["members"]], [3, 2, 1])
        self.assertEqual(self.clients, [(REPOSITORY, False, limits.MAX_CI_BATCH_PREPARE_REQUESTS)])
        self.assertEqual((world.hub.request_count, world.hub.writes(), world.remote.refs()), (3 + 3, [], refs))
        self.assertEqual(self.outputs(), {"branch": "batch/dry", "pr_number": "",
                                          "head_sha": result["manifest"]["members"][-1]["squash_sha"]})

    def test_prepare_sends_ten_requests_and_three_per_member(self):
        self.assertEqual(limits.MAX_CI_BATCH_PREPARE_REQUESTS, 216)
        for count in (3, limits.MAX_CI_BATCH_MEMBERS):
            world = self.world(limits.MAX_CI_BATCH_PREPARE_REQUESTS)
            numbers = self.members(world, count)
            result = self.run_verb(world, "batch-prepare", "--name", "run-1", "--allowed-paths", str(self.allowed),
                                   *numbers)
            with self.subTest(count=count):
                self.assertEqual(world.hub.request_count, 10 + 3 * count)  # 19 and 160
                self.assertLess(world.hub.request_count, 20 * count)
                self.assertEqual(self.clients, [(REPOSITORY, True, limits.MAX_CI_BATCH_PREPARE_REQUESTS)])
                self.assertEqual(world.hub.writes(), [("POST", f"/repos/{REPOSITORY}/pulls")])
                head = result["manifest"]["members"][-1]["squash_sha"]
                self.assertEqual(world.remote.rev("refs/heads/batch/run-1"), head)
                self.assertEqual(self.outputs(), {"branch": "batch/run-1", "head_sha": head,
                                                  "pr_number": str(result["pr_number"])})
                self.assertEqual(result["pr_number"], count + 1)

    def test_settle_sends_three_requests_the_gate_pair_and_at_most_five_per_member(self):
        self.assertEqual(limits.MAX_CI_BATCH_SETTLE_REQUESTS, 348)
        for count, delete in ((3, False), (3, True), (limits.MAX_CI_BATCH_MEMBERS, True)):
            world = self.world(None)
            numbers = self.members(world, count)
            with world.store() as store:
                prepared = commands_batch.batch.prepare_batch(world.hub, store, name="run-1", allowed_paths=ALLOWED,
                                                              pr_numbers=tuple(int(number) for number in numbers))
            arguments = world.merge(prepared)
            files = {}
            for name in ("plan", "build_seal", "packaged_seal"):
                files[name] = world.root / f"{name}.json"
                files[name].write_bytes(canonical_json(arguments[name]))
            world.hub.calls.clear()
            start = world.hub.request_count
            self.output.unlink(missing_ok=True)
            report = self.run_verb(world, "batch-settle", "--pr", str(prepared["pr_number"]), "--plan",
                                   str(files["plan"]), "--build-seal", str(files["build_seal"]), "--packaged-seal",
                                   str(files["packaged_seal"]), *(["--delete-branches"] if delete else []))
            with self.subTest(count=count, delete=delete):
                spent = world.hub.request_count - start
                self.assertEqual(spent, 3 + GATE_PAIR_REQUESTS + (5 if delete else 3) * count)  # 40, 46 and 281
                self.assertLessEqual(spent, limits.MAX_CI_BATCH_SETTLE_REQUESTS)
                if count == 3:  # a batch of a few members stays within the target of every command
                    self.assertLess(spent, 60)
                self.assertEqual(self.clients[-1], (REPOSITORY, True, limits.MAX_CI_BATCH_SETTLE_REQUESTS))
                everyone = list(range(1, count + 1))
                self.assertEqual(report, {"closed": everyone, "changed": [], "already_closed": [],
                                          "deleted": everyone if delete else []})
                self.assertEqual(self.outputs(), {"closed": str(count), "changed": "0", "already_closed": "0",
                                                  "deleted": str(count if delete else 0)})
                self.assertEqual(len(world.hub.writes()), (3 if delete else 2) * count)
                self.assertEqual(world.remote.rev("refs/heads/fix/n1") is None, delete)

    def test_unreadable_inputs_stop_before_any_request(self):
        world = self.world(limits.MAX_CI_BATCH_PREPARE_REQUESTS)
        numbers = self.members(world, 1)
        for content in (b'{"paths": ["src"]}', b'"src"', b"", b'["src", "alpha.txt"]', b'[7]', b'["src",'):
            self.allowed.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(MbError):
                self.run_verb(world, "batch-prepare", "--name", "n", "--allowed-paths", str(self.allowed), *numbers)
        with self.assertRaises(MbError):
            self.run_verb(world, "batch-prepare", "--name", "n", "--allowed-paths", str(world.root / "none"), *numbers)
        with self.assertRaises(MbError):
            self.run_verb(world, "batch-settle", "--pr", "9", "--plan", str(world.root / "none"), "--build-seal",
                          str(self.allowed), "--packaged-seal", str(self.allowed))
        self.assertEqual((world.hub.request_count, world.remote.rev("refs/heads/batch/n")), (0, None))


if __name__ == "__main__":
    unittest.main()
