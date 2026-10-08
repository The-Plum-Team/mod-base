"""The candidate path below the accounts: inventory, kit overlay, state records, arguments.

``ci worker-stage``, ``ci worker-run`` and ``ci worker-seal`` up to the point where they need a
disposable account. Real files, Git and processes in temporary directories; only the GitHub API
is a fake. What needs accounts, sudo and root operations is ``LinuxCandidateCommandTests`` in
``tests/ci_linux_worker.py``. A worker passed to a lifecycle function here names accounts that do
not exist: every case must be refused before one is used.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base import __version__, runtime
from mod_base.build_ci import identity, lifecycle, worker_overlay
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.root_request_operations import candidate_generated_roots
from mod_base.build_ci.source import verify_source_copy
from mod_base.build_ci.toolchain import ToolTreeProof
from mod_base.build_ci.worker import WorkerAccount
from mod_base.build_ci.worker_preparation import _tree_id
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import (ACTIONS_LOCK, STAGED_LOCK, STAMP_NAME, actions_listing, kit_tree_digest, staged_listing,
                          parse_pin, stamp_document, verify_released)
from tests import ci_candidate_fixture as candidate_fixture
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h

SUBJECT_REQUESTS = 4
ROOT = "/tmp/mod-base-sandbox-boundary/mod-base-worker"


class CandidateCase(unittest.TestCase):
    """A job of the fixture pull request whose tested commit is a fetched, detached checkout."""

    def setUp(self) -> None:
        if not Path(fixture.GIT).exists():
            self.skipTest("git is not installed at /usr/bin/git")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.mod = h.materialize(self.temporary / "mod")
        self.state = self.temporary / "state"
        self.output = self.temporary / "github-output"
        self.api, self.pull = h.github()
        self.environment = h.environment()

    def begin(self, tested: Path | None = None, *, producer: str = "build") -> lifecycle.Job:
        """Fetch ``tested`` (by default the protected mod) as the candidate checkout, make its
        commit the test merge and run ``ci subject``."""
        self.checkout, self.commit, self.tree = candidate_fixture.fetch_checkout(tested or self.mod, self.temporary)
        fixture.retarget(self.api, self.pull, self.commit, self.tree)
        self.environment = {**self.environment, **h.environment(caller=producer)}
        self.commands = fixture.Commands(self.mod, self.state, self.api, self.environment)
        self.assertEqual(self.commands.run("subject", "--producer", producer, "--pr", "7", "--github-output",
                                           str(self.output)), (0, "", ""))
        return self.job()

    def job(self) -> lifecycle.Job:
        return lifecycle.open_job(runtime.build_invocation(self.mod, None, self.environment), self.state)

    def plan(self, job: lifecycle.Job) -> dict:
        """Record the plan the pure planning functions derive for the job's subject."""
        sandbox = h.Sandbox(self.temporary / "pure", protected=self.mod)
        sandbox.subject = job.subject
        self.sandbox = sandbox
        plan = sandbox.derive_plan()
        identity.write_state_record(self.state, lifecycle.PLAN_NAME, canonical_json(plan))
        return lifecycle.read_plan(job)

    def worker(self, job: lifecycle.Job, *, candidate: bool = True) -> lifecycle.Worker:
        """A worker record as ``ci worker-prepare`` leaves one, for accounts this machine does not have."""
        accounts = {"validator": WorkerAccount("validator", 2001, 2001, f"{ROOT}/validator-home")}
        if candidate:
            accounts = {"candidate": WorkerAccount("candidate", 2000, 2000, f"{ROOT}/candidate-home"), **accounts}
        return lifecycle.Worker(HostBoundary("/home/runner", os.getuid(), os.getgid(), 1, 10, 0o750), accounts,
                                "/opt/python/bin/python3", (), ToolTreeProof(("/opt/python",), "0" * 64, 1, 2, 3),
                                job.config.sha256)

    def records(self) -> set[str]:
        return {path.name for path in self.state.iterdir()}

    def write(self, name: str, document: object) -> None:
        identity.write_state_record(self.state, name, canonical_json(document))


class InventoryTests(CandidateCase):
    def mod_with_every_mode(self) -> Path:
        """A tested tree with every kind of entry Git tracks for a mod: an executable and a link."""
        mod = h.materialize(self.temporary / "tested")
        (mod / "gradlew").write_bytes(b"#!/bin/sh\nexec true\n")
        (mod / "gradlew").chmod(0o755)
        (mod / "docs").mkdir()
        (mod / "docs/latest").symlink_to("../gradle.properties")
        (mod / "src/empty").write_bytes(b"")
        return mod

    def authored(self, mod: Path) -> list[tuple[str, str, int, str]]:
        """``(path, mode, size, blob id)`` of every file below ``mod``, from its bytes alone."""
        rows = []
        for path in sorted(item for item in mod.rglob("*") if item.is_symlink() or item.is_file()):
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            mode = "120000" if path.is_symlink() else ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644")
            rows.append((path.relative_to(mod).as_posix(), mode, len(data),
                         hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()))  # noqa: S324 - Git object id
        return sorted(rows)

    def test_the_inventory_is_the_tested_tree_as_its_git_objects_list_it(self) -> None:
        mod = self.mod_with_every_mode()
        job = self.begin(mod)
        inventory = lifecycle.checkout_inventory(self.checkout, job)
        self.assertEqual([(entry.path, entry.mode, entry.size, entry.git_blob) for entry in inventory],
                         self.authored(mod))
        self.assertEqual({entry.mode for entry in inventory}, {"100644", "100755", "120000"})
        # It names the authenticated tree and no other, and the checkout that was fetched holds it.
        self.assertEqual(_tree_id(inventory), job.subject["tested_tree"])
        self.assertEqual(len(verify_source_copy(self.checkout, inventory=inventory)), len(inventory))
        self.assertEqual(lifecycle.checkout_inventory(Path(os.path.relpath(self.checkout)), job), inventory)
        # Only objects are read: the working files may be anything.
        (self.checkout / "gradle.properties").write_bytes(b"changed\n")
        (self.checkout / "untracked.txt").write_bytes(b"new\n")
        self.assertEqual(lifecycle.checkout_inventory(self.checkout, job), inventory)
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS)

    def test_a_replacement_object_never_stands_in_for_the_tested_tree(self) -> None:
        job = self.begin()
        inventory = lifecycle.checkout_inventory(self.checkout, job)
        (self.temporary / "forged").write_bytes(b"forged\n")
        forged = fixture.git(self.checkout, "hash-object", "-w", str(self.temporary / "forged"))
        fixture.git(self.checkout, "replace", inventory[0].git_blob, forged)
        other = fixture.git(self.checkout, "mktree", "--missing", "-z")  # An empty tree in place of the tested one.
        fixture.git(self.checkout, "replace", self.tree, other)
        self.assertEqual(lifecycle.checkout_inventory(self.checkout, job), inventory)

    def test_a_checkout_on_a_branch_or_at_another_commit_is_refused(self) -> None:
        job = self.begin()
        fixture.git(self.checkout, "checkout", "-q", "-b", "work")
        self.assertEqual(fixture.git(self.checkout, "rev-parse", "HEAD"), self.commit)
        with self.assertRaisesRegex(MbError, "not detached at the tested commit"):
            lifecycle.checkout_inventory(self.checkout, job)
        (self.checkout / "gradle.properties").write_bytes(b"moved on\n")
        fixture.git(self.checkout, "commit", "-q", "-a", "-m", "moved on")
        with self.assertRaisesRegex(MbError, "not at the tested commit"):
            lifecycle.checkout_inventory(self.checkout, job)
        fixture.git(self.checkout, "checkout", "-q", "--detach", self.commit)
        self.assertEqual(len(lifecycle.checkout_inventory(self.checkout, job)), len(self.authored(self.mod)))
        for directory in (self.temporary / "absent", self.temporary, self.checkout / "scripts"):
            with self.subTest(directory=directory.name), self.assertRaisesRegex(MbError, "no Git directory of its own"):
                lifecycle.checkout_inventory(directory, job)

    def test_a_tree_git_cannot_stage_as_plain_files_is_refused(self) -> None:
        cases = {}
        mod = h.materialize(self.temporary / "submodule")
        cases["a submodule"] = (mod, lambda checkout: fixture.git(
            checkout, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/library"))
        mod = h.materialize(self.temporary / "spaces")
        (mod / "release notes.txt").write_bytes(b"notes\n")
        cases["a path outside the repository grammar"] = (mod, None)
        mod = h.materialize(self.temporary / "aliases")
        (mod / "docs").mkdir()
        (mod / "docs/Notes.txt").write_bytes(b"one\n")
        (mod / "docs/notes.txt").write_bytes(b"two\n")
        cases["two paths that differ only in case"] = (mod, None)
        for index, (label, (mod, stage)) in enumerate(cases.items()):
            with self.subTest(label=label):
                base = self.temporary / f"case-{index}"
                base.mkdir()
                upstream = base / "upstream"
                shutil.copytree(mod, upstream, symlinks=True)
                fixture.git(upstream, "init", "-q")
                fixture.git(upstream, "add", "-A")
                if stage is not None:
                    stage(upstream)
                fixture.git(upstream, "commit", "-q", "-m", "candidate")
                commit, tree = (fixture.git(upstream, "rev-parse", name) for name in ("HEAD", "HEAD^{tree}"))
                fixture.git(upstream, "checkout", "-q", "--detach", commit)
                api, pull = h.github()
                fixture.retarget(api, pull, commit, tree)
                state = base / "state"
                commands = fixture.Commands(self.mod, state, api, self.environment)
                commands.subject(base / "output")
                job = lifecycle.open_job(runtime.build_invocation(self.mod, None, self.environment), state)
                with self.assertRaises(MbError):
                    lifecycle.checkout_inventory(upstream, job)

    def test_generated_roots_are_the_kit_overlay_and_the_bundle_directory_of_the_protected_config(self) -> None:
        job = self.begin()
        self.assertEqual(candidate_generated_roots(job.config.data), ("build/release", "out/mod-base-kit"))
        for path in ("out/mod-base-kit", "out", "OUT/mod-base-kit/stage", "out/mod-base-kit/build"):
            config = copy.deepcopy(job.config.data)
            config["bundle"]["path"] = path
            with self.subTest(path=path), self.assertRaisesRegex(MbError, "apart from the kit overlay"):
                candidate_generated_roots(config)
        config = copy.deepcopy(job.config.data)
        config["bundle"]["path"] = "out/release"  # Beside the overlay, not in it.
        self.assertEqual(candidate_generated_roots(config), ("out/mod-base-kit", "out/release"))


class KitOverlayTests(CandidateCase):
    def resolve_staged_candidate(self, sha: str, version: str) -> None:
        """Run the managed bootstrap over the actual overlay a candidate job supplies."""
        tested = h.materialize(self.temporary / "tested")
        (tested / ".github/workflows/kit.yml").write_text(
            "name: Kit pin\non: workflow_dispatch\njobs:\n  kit:\n"
            f"    uses: The-Plum-Team/mod-base/.github/workflows/build.yml@{sha} # {version}\n",
            encoding="utf-8", newline="\n")
        job = self.begin(tested)
        # Even a candidate pin whose immutable tag and ancestry pass the existing
        # protected admission cannot resolve the overlay of a different executing pin.
        api = FakeGitHub(repository="The-Plum-Team/mod-base")
        api.add_compare(sha, "main", {"status": "ahead", "behind_by": 0, "ahead_by": 1})
        api.add_ref("tags/" + version, sha, annotated_tag_sha="5" * 40)
        verify_released(parse_pin(self.checkout), api)
        overlay = self.checkout / "out/mod-base-kit"
        lifecycle.stage_kit_overlay(job, overlay)
        result = subprocess.run(
            (sys.executable, "-I", "-B", str(job.kit_root / "template/managed/scripts/ci/mod_base_kit.py"),
             "path", "--repo", str(self.checkout)),
            cwd=self.checkout, env={"HOME": str(self.temporary), "CI": "true", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(overlay))

    def test_an_unchanged_candidate_pin_resolves_the_lifecycle_overlay(self) -> None:
        self.resolve_staged_candidate(h.KIT_SHA, "v" + __version__)

    # Scope decision: ordinary K1-K6 generations keep the protected pin. Supporting a
    # candidate's future pin needs separate overlay admission before Q/B adopts that
    # upgrade route; do not relax bootstrap verification or change the executing pin.
    # See BUILD-PROTOCOL.md, "Candidate kit pin limitation".
    @unittest.expectedFailure  # Candidate overlay admission must distinguish the future pin from the executing pin.
    def test_a_candidate_kit_bump_resolves_the_lifecycle_overlay(self) -> None:
        self.resolve_staged_candidate("4" * 40, "v1.0.4")

    def test_the_staged_kit_is_the_verified_checkout_with_the_stamp_root_admits(self) -> None:
        job = self.begin()
        overlay = self.temporary / "kit"
        pin = lifecycle.stage_kit_overlay(job, overlay)
        self.assertEqual((pin.sha, pin.version, pin.references), (h.KIT_SHA, f"v{job.subject['kit']['version']}", ()))
        self.assertEqual(sorted(os.listdir(overlay)),
                         sorted(["src", "site", "requirements", "template", "tools", "actions", STAMP_NAME]))
        self.assertEqual((overlay / STAMP_NAME).read_bytes(), canonical_json(stamp_document(pin, job.kit_digest)))
        # Root's own admission of an overlay accepts it for that pin and the verified digest.
        records = worker_overlay._admit(overlay, pin, job.kit_digest)
        kit = runtime.kit_root()
        staged = {record["path"]: record["sha256"] for record in records}
        for name in ("src/mod_base/__init__.py", "tools/ci_privileged_bootstrap.py", "site/mod-base.schema.json"):
            if (kit / name).is_file():
                self.assertEqual(staged[name], hashlib.sha256((kit / name).read_bytes()).hexdigest(), name)
        self.assertIn("src/mod_base/build_ci/lifecycle.py", staged)
        self.assertFalse(any(name.startswith(("tests/", "docs/", ".git")) for name in staged))
        for path in overlay.rglob("*"):
            self.assertFalse(path.is_symlink(), path)
            if path.is_file():
                self.assertEqual((stat.S_IMODE(path.stat().st_mode), path.stat().st_nlink), (0o644, 1), path)
        self.assertNotEqual((overlay / "src/mod_base/__init__.py").stat().st_ino,
                            (kit / "src/mod_base/__init__.py").stat().st_ino)
        with self.assertRaisesRegex(MbError, "refusing to replace existing output"):
            lifecycle.stage_kit_overlay(job, overlay)

    def small_kit(self, root: Path) -> str:
        """The smallest kit checkout with every staged directory and both locks; returns its digest."""
        files = {"src/mod_base/__init__.py": b"__version__ = '1.0.3'\n", "site/index.html": b"<p>site</p>\n",
                 "requirements/pillow.txt": b"pillow\n", "template/managed/README.md": b"managed\n",
                 "tools/tool.py": b"print('tool')\n", "actions/setup/action.yml": b"name: setup\n"}
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        (root / STAGED_LOCK).parent.mkdir(parents=True, exist_ok=True)
        (root / ACTIONS_LOCK).write_bytes(actions_listing(root))
        (root / STAGED_LOCK).write_bytes(staged_listing(root))
        return kit_tree_digest(root)

    def test_a_kit_that_is_not_the_verified_one_is_never_staged(self) -> None:
        job = self.begin()
        fields = {name: getattr(job, name) for name in job.__dataclass_fields__}
        changes = {
            "a changed digested file": lambda kit: (kit / "site/index.html").write_bytes(b"changed\n"),
            "an added digested file": lambda kit: (kit / "requirements/extra.txt").write_bytes(b"x"),
            "bytecode": lambda kit: [(kit / "src/mod_base/__pycache__").mkdir(),
                                     (kit / "src/mod_base/__pycache__/x.pyc").write_bytes(b"x")],
            "a tool outside the staged-file lock": lambda kit: (kit / "tools/extra.py").write_bytes(b"x"),
            "an action outside its lock": lambda kit: (kit / "actions/setup/extra.sh").write_bytes(b"x"),
            "a missing directory": lambda kit: shutil.rmtree(kit / "actions"),
            "a link": lambda kit: (kit / "tools/link.py").symlink_to(kit / "tools/tool.py"),
            "a second name": lambda kit: os.link(kit / "tools/tool.py", self.temporary / "alias.py"),
        }
        for index, (label, change) in enumerate(changes.items()):
            with self.subTest(label=label):
                kit = self.temporary / f"kit-{index}"
                small = lifecycle.Job(**{**fields, "kit_root": kit, "kit_digest": self.small_kit(kit)})
                faithful = self.temporary / f"faithful-{index}"
                pin = lifecycle.stage_kit_overlay(small, faithful)
                self.assertEqual(len(worker_overlay._admit(faithful, pin, small.kit_digest)), 9)
                change(kit)
                with self.assertRaises(MbError):
                    lifecycle.stage_kit_overlay(small, self.temporary / f"overlay-{index}")
                self.assertFalse((self.temporary / f"overlay-{index}").exists())
                self.assertEqual([path.name for path in self.temporary.iterdir() if ".building-" in path.name], [])
                (self.temporary / "alias.py").unlink(missing_ok=True)


class StateRecordTests(CandidateCase):
    STAGE = {"candidate": "/home/runner/work/mod/mod/candidate", "tested_sha": None, "tested_tree": None,
             "source": {"files": 11, "bytes": 4096},
             "kit": {"sha": h.KIT_SHA, "version": "1.0.3", "tree_digest": "sha256:" + "4" * 64},
             "gradle_seed": False, "bundle": None}
    RUN = {"hook": "build_target", "unit_id": "1.20.1", "succeeded": True, "error": None, "returncode": 0,
           "truncated": False, "log_bytes": 12, "log_sha256": "a" * 64}
    SEAL = {"hook": "build_target", "unit_id": "1.20.1", "export": "build", "envelope_sha256": "b" * 64, "files": 8}

    def stage(self, job: lifecycle.Job, **changes: object) -> dict:
        return {**self.STAGE, "tested_sha": job.subject["tested_sha"], "tested_tree": job.subject["tested_tree"],
                **changes}

    def refused(self, name: str, read, valid: dict, changes: list[dict]) -> None:
        """Every changed document is refused when it is the record ``name``; the valid one is read back."""
        for change in changes:
            document = {**valid, **change}
            document = {key: value for key, value in document.items() if value is not ...}
            self.write(name, document)
            with self.subTest(name=name, change=change), self.assertRaises(MbError):
                read()
            (self.state / name).unlink()
        for raw in (json.dumps(valid).encode("utf-8"), canonical_json(valid) + b"\n", b"[]", b""):
            (self.state / name).write_bytes(raw)
            (self.state / name).chmod(0o600)
            with self.subTest(name=name, raw=raw[:20]), self.assertRaises(MbError):
                read()
            (self.state / name).unlink()
        self.write(name, valid)
        (self.state / name).chmod(0o644)  # Readable by others: not a state record.
        with self.assertRaises(MbError):
            read()
        (self.state / name).unlink()
        self.write(name, valid)
        self.assertEqual(read(), valid)

    def test_the_stage_record_names_what_the_candidate_was_staged_from(self) -> None:
        job = self.begin()
        bundle = {"path": "build/release", "envelope_sha256": "c" * 64, "files": 12}
        with self.assertRaisesRegex(MbError, "cannot read the state record worker-stage.json"):
            lifecycle.read_stage(job)
        self.refused(lifecycle.STAGE_NAME, lambda: lifecycle.read_stage(job), self.stage(job, bundle=bundle), [
            {"tested_sha": "e" * 40}, {"tested_tree": "e" * 40}, {"candidate": "candidate"},
            {"candidate": "/home/runner/work/../candidate"}, {"candidate": ...}, {"source": {"files": 0, "bytes": 0}},
            {"source": {"files": 11}}, {"kit": {"sha": h.KIT_SHA, "version": "v1.0.3", "tree_digest": "sha256:" + "4" * 64}},
            {"gradle_seed": None}, {"gradle_seed": "/home/runner/seed"}, {"bundle": {**bundle, "path": "../release"}},
            {"bundle": {**bundle, "files": 0}}, {"bundle": {"path": "build/release"}}, {"bundle": False},
            {"inventory": []}, {"overlay": "/home/runner/kit"}])

    def test_the_run_record_says_how_the_one_candidate_hook_ended(self) -> None:
        job = self.begin()
        failed = {**self.RUN, "succeeded": False, "error": "worker execution timed out", "returncode": None,
                  "log_bytes": 0}
        for valid in (failed, {**failed, "returncode": 0, "error": "worker dispatcher left a process behind"},
                      {**self.RUN, "hook": "policy", "unit_id": None},
                      {**self.RUN, "hook": "run_lane", "unit_id": "fabric-1.20.1", "truncated": True}):
            self.write(lifecycle.RUN_NAME, valid)
            self.assertEqual(lifecycle.read_run(job), valid)
            (self.state / lifecycle.RUN_NAME).unlink()
        self.refused(lifecycle.RUN_NAME, lambda: lifecycle.read_run(job), self.RUN, [
            {"hook": "verify_target"}, {"hook": "derive_runtime"}, {"hook": "policy"}, {"unit_id": None},
            {"unit_id": "Fabric 1.20.1"}, {"succeeded": 1}, {"succeeded": False},
            {"error": "the hook failed"}, {"returncode": 1}, {"returncode": None}, {"returncode": True},
            {"error": "two\nlines", "succeeded": False}, {"error": "", "succeeded": False},
            {"error": "x" * 1001, "succeeded": False}, {"truncated": 0}, {"log_bytes": -1},
            {"log_bytes": limits.MAX_CI_LOG_BYTES + 1}, {"log_sha256": "A" * 64}, {"log_sha256": ...},
            {"log": "candidate output"}, {"command": ["sh"]}])

    def test_the_seal_record_names_the_export_of_its_hook_and_its_envelope(self) -> None:
        job = self.begin()
        nothing = {"hook": "policy", "unit_id": None, "export": None, "envelope_sha256": None, "files": 0}
        for valid in (nothing, {**self.SEAL, "hook": "run_lane", "unit_id": "fabric-1.20.1", "export": "runtime"}):
            self.write(lifecycle.SEAL_NAME, valid)
            self.assertEqual(lifecycle.read_seal(job), valid)
            (self.state / lifecycle.SEAL_NAME).unlink()
        self.refused(lifecycle.SEAL_NAME, lambda: lifecycle.read_seal(job), self.SEAL, [
            {"export": "runtime"}, {"export": None}, {"export": "validation"}, {"hook": "run_lane"},
            {"hook": "policy"}, {"envelope_sha256": None}, {"files": 0}, {"files": limits.MAX_CI_EXPORT_FILES + 1},
            {"envelope_sha256": "sha256:" + "b" * 64}, {"unit_id": ...}, {"path": f"{ROOT}/sealed-build"}])

    def test_the_selection_record_must_be_the_one_this_run_attempt_requested(self) -> None:
        job = self.begin(producer="packaged")
        plan = self.plan(job)
        descriptor = candidate_fixture.build_descriptor(plan)
        envelope = candidate_fixture.build_complete(self.sandbox, self.temporary / "build", descriptor)
        selection = candidate_fixture.selection(plan, descriptor, envelope, run_id=42)
        with self.assertRaisesRegex(MbError, "cannot read the state record ci-selection.json"):
            lifecycle.read_selection(job, plan)
        self.write(lifecycle.SELECTION_NAME, selection)
        self.assertEqual(lifecycle.read_selection(job, plan), selection)
        (self.state / lifecycle.SELECTION_NAME).unlink()
        for label, document in {
                "another run": candidate_fixture.selection(plan, descriptor, envelope, run_id=43),
                "another attempt": candidate_fixture.selection(plan, descriptor, envelope, run_id=42, run_attempt=2),
                "the Build caller": candidate_fixture.selection(plan, descriptor, envelope, run_id=42, caller="build"),
                "another plan": {**selection, "plan_sha256": "f" * 64},
                "a target partition": {**selection, "build": {**descriptor, "artifact": {
                    **descriptor["artifact"], "name": "mb-ci-target--41--a1--1.20.1"}}},
                "an unknown key": {**selection, "path": "sealed-build"}}.items():
            self.write(lifecycle.SELECTION_NAME, document)
            with self.subTest(label=label), self.assertRaises(MbError):
                lifecycle.read_selection(job, plan)
            (self.state / lifecycle.SELECTION_NAME).unlink()

    def test_the_producer_of_a_record_is_the_executing_run_in_the_mode_its_graph_shows(self) -> None:
        job = self.begin(producer="packaged")
        plan = self.plan(job)
        self.assertEqual(lifecycle._producer(job, plan, "pull-request"),
                         candidate_fixture.producer(plan, "packaged", "pull-request", run_id=42))
        build = candidate_fixture.build_descriptor(plan)
        self.assertEqual(lifecycle._lane_mode(job, build), "pull-request")
        # A protected subject either selected the Build of a Build run or built it in this very run.
        protected = lifecycle.Job(**{**{name: getattr(job, name) for name in job.__dataclass_fields__}, "record": {
            **job.record, "event": "push", "subject": candidate_fixture.protected_plan(plan)["identity"]}})
        pushed = candidate_fixture.protected_plan(plan)
        self.assertEqual(lifecycle._lane_mode(protected, candidate_fixture.build_descriptor(pushed, event="push")),
                         "selected")
        rebuilt = candidate_fixture.build_descriptor(pushed, caller="packaged", mode="rebuilt", run_id=42, event="push")
        self.assertEqual(lifecycle._lane_mode(protected, rebuilt), "rebuilt")
        for run in ({"run_id": 41}, {"run_attempt": 2}):
            foreign = candidate_fixture.build_descriptor(pushed, caller="packaged", mode="rebuilt", event="push",
                                                         **{"run_id": 42, **run})
            with self.subTest(run=run), self.assertRaisesRegex(MbError, "this run attempt built"):
                lifecycle._lane_mode(protected, foreign)


class StepOrderTests(CandidateCase):
    """What each step refuses before it uses an account: the worker here names none that exists."""

    def test_the_stage_refuses_a_job_without_a_candidate_a_second_stage_and_an_unfaithful_checkout(self) -> None:
        job = self.begin()
        plan = self.plan(job)

        def stage(worker=None, checkout=None, **options):
            arguments = {"gradle_seed": None, "bundle": False, **options}
            return lifecycle.stage_candidate(job, worker or self.worker(job), plan, checkout or self.checkout,
                                             **arguments)

        with self.assertRaisesRegex(MbError, "allocated no candidate account"):
            stage(self.worker(job, candidate=False))
        (self.checkout / "notes.txt").write_bytes(b"untracked\n")
        with self.assertRaisesRegex(MbError, "source contains an undeclared path"):
            stage()
        (self.checkout / "notes.txt").unlink()
        (self.checkout / "build").mkdir()  # Not even an empty directory beside the tracked files.
        with self.assertRaisesRegex(MbError, "source contains an undeclared path"):
            stage()
        (self.checkout / "build").rmdir()
        payload = self.checkout / "src/payload.txt"
        original = payload.read_bytes()
        payload.write_bytes(original + b"edited\n")
        with self.assertRaisesRegex(MbError, "tracked source differs from the protected tested tree"):
            stage()
        payload.write_bytes(original)
        payload.chmod(0o755)
        with self.assertRaisesRegex(MbError, "tracked source differs from the protected tested tree"):
            stage()
        payload.chmod(0o644)
        payload.unlink()
        with self.assertRaises(MbError):
            stage()
        payload.write_bytes(original)
        fixture.git(self.checkout, "checkout", "-q", "-b", "work")
        with self.assertRaisesRegex(MbError, "not detached at the tested commit"):
            stage()
        fixture.git(self.checkout, "checkout", "-q", "--detach", self.commit)
        with self.assertRaisesRegex(MbError, "no Git directory of its own"):
            stage(checkout=self.mod)
        self.assertEqual(self.records(), {"identity.json", "ci-plan.json"})
        self.write(lifecycle.STAGE_NAME, StateRecordTests.STAGE | {"tested_sha": job.subject["tested_sha"],
                                                                 "tested_tree": job.subject["tested_tree"]})
        with self.assertRaisesRegex(MbError, "already staged its candidate"):
            stage()
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS)

    def test_the_run_refuses_what_is_not_the_one_staged_hook_of_this_job(self) -> None:
        job = self.begin()
        plan = self.plan(job)
        log: list[str] = []

        def run(hook, unit_id=None, worker=None):
            return lifecycle.run_candidate_hook(job, worker or self.worker(job), plan, hook, unit_id=unit_id,
                                                log=log.append)

        stage = StateRecordTests.STAGE | {"tested_sha": job.subject["tested_sha"],
                                          "tested_tree": job.subject["tested_tree"]}
        with self.assertRaisesRegex(MbError, "cannot read the state record worker-stage.json"):
            run("policy")
        self.write(lifecycle.STAGE_NAME, stage)
        for hook, unit, message in (("verify_target", "1.20.1", "is not a candidate hook"),
                                    ("derive_runtime", "fabric-1.20.1", "is not a candidate hook"),
                                    ("build_target", "1.19.4", "outside the protected plan"),
                                    ("build_target", "fabric-1.20.1", "outside the protected plan"),
                                    ("build_target", None, "outside the protected plan"),
                                    ("policy", "1.20.1", "runs for no unit"),
                                    ("run_lane", "fabric-1.20.1", "run_lane is a step of a packaged job")):
            with self.subTest(hook=hook, unit=unit), self.assertRaisesRegex(MbError, message):
                run(hook, unit)
        with self.assertRaisesRegex(MbError, "allocated no candidate account"):
            run("policy", worker=self.worker(job, candidate=False))
        self.write(lifecycle.RUN_NAME, StateRecordTests.RUN)
        with self.assertRaisesRegex(MbError, "already ran its candidate hook"):
            run("policy")
        self.assertEqual((log, self.records()),
                         ([], {"identity.json", "ci-plan.json", "worker-stage.json", "worker-run.json"}))

    def test_a_lane_runs_in_a_packaged_job_whose_stage_placed_the_build(self) -> None:
        job = self.begin(producer="packaged")
        plan = self.plan(job)
        self.write(lifecycle.STAGE_NAME, StateRecordTests.STAGE | {"tested_sha": job.subject["tested_sha"],
                                                                 "tested_tree": job.subject["tested_tree"]})
        for hook, unit, message in (("run_lane", "fabric-1.20.1", "ci worker-stage --bundle"),
                                    ("run_lane", "1.20.1", "outside the protected plan"),
                                    ("policy", None, "policy is a step of a build job"),
                                    ("build_target", "1.20.1", "build_target is a step of a build job")):
            with self.subTest(hook=hook), self.assertRaisesRegex(MbError, message):
                lifecycle.run_candidate_hook(job, self.worker(job), plan, hook, unit_id=unit, log=self.fail)
        self.assertNotIn("worker-run.json", self.records())

    def test_the_seal_needs_the_candidate_account_before_it_reads_anything(self) -> None:
        self.begin()
        with mock.patch.object(lifecycle, "worker_account_exists", return_value=False), \
                mock.patch.object(lifecycle, "terminate_worker") as terminate, \
                mock.patch.object(lifecycle, "open_job") as opened:
            self.assertEqual(self.commands.run("worker-seal"),
                             (2, "", "mod_base: ci-lifecycle: this job has no candidate account to seal\n"))
        terminate.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(self.records(), {"identity.json"})


class ArgumentTests(CandidateCase):
    """Usage and admission of the three verbs; none of these cases may reach the host."""

    def test_malformed_flags_are_usage_errors_before_anything_is_read(self) -> None:
        self.begin()
        cases = [("worker-stage",), ("worker-stage", "--candidate", ""), ("worker-stage", "--candidate"),
                 ("worker-stage", "--candidate", "c", "--bundle", "build/release"),
                 ("worker-stage", "--candidate", "c", "--gradle-seed", ""),
                 ("worker-stage", "--candidate", "c", "--overlay", "kit"),
                 ("worker-stage", "--candidate", "c", "--roles", "candidate+validator"),
                 ("worker-run",), ("worker-run", "--hook", "verify_target", "--unit", "1.20.1"),
                 ("worker-run", "--hook", "derive_plan"), ("worker-run", "--hook", "policy", "--unit", "1.20.1"),
                 ("worker-run", "--hook", "build_target"), ("worker-run", "--hook", "run_lane"),
                 ("worker-run", "--hook", "build_target", "--unit", ""),
                 ("worker-run", "--hook", "build_target", "--unit", "1.20.1/../x"),
                 ("worker-run", "--hook", "run_lane", "--unit", "fabric--1.20.1"),
                 ("worker-run", "--hook", "policy", "--timeout", "5"), ("worker-run", "--hook", "policy", "sh"),
                 ("worker-seal", "--export", "build/release"), ("worker-seal", "--hook", "policy"),
                 ("worker-seal", "extra")]
        with mock.patch.object(lifecycle, "worker_account_exists") as exists:
            for arguments in cases:
                code, stdout, stderr = self.commands.run(*arguments)
                with self.subTest(arguments=arguments):
                    self.assertEqual((code, stdout), (2, ""))
                    self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
                    self.assertEqual(stderr.count("\n"), 1)
        exists.assert_not_called()
        self.assertEqual(self.records(), {"identity.json"})
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))

    def test_stage_and_run_need_the_state_and_the_prepared_worker_of_their_job(self) -> None:
        missing = self.temporary / "no-state"
        commands = fixture.Commands(self.mod, missing, self.api, self.environment)
        for arguments in (("worker-stage", "--candidate", "candidate"), ("worker-run", "--hook", "policy")):
            code, stdout, stderr = commands.run(*arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("ci-state: cannot open the state directory", stderr)
        self.assertFalse(missing.exists())
        self.begin()
        for arguments in (("worker-stage", "--candidate", str(self.checkout)), ("worker-run", "--hook", "policy")):
            code, stdout, stderr = self.commands.run(*arguments)  # No worker was prepared.
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("ci-state: cannot read the state record worker.json", stderr)
        self.commands.environment = {**self.environment, "GITHUB_SHA": "e" * 40}
        for arguments in (("worker-stage", "--candidate", str(self.checkout)), ("worker-run", "--hook", "policy")):
            code, stdout, stderr = self.commands.run(*arguments)
            with self.subTest(other_run=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("the state directory belongs to another repository or controller commit", stderr)
        self.assertEqual(self.records(), {"identity.json"})


if __name__ == "__main__":
    unittest.main()
