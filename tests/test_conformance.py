"""``conformance`` (MB10, SPEC §9.1), the canary (§9.2) and the kit's own CI workflow (§5.10).

The simulation runs for real, in its credential-free child process, against the canary and the
kit's fixture mods: the canary with its synthetic family (every family outcome, carried across three
heads), the Block Pops-like fixture (job graph, display title, attested reuse, anchor successor
grace) and the unmodified Quick Skin-like fixture with its own optional conformance fixtures
(delegated reuse whose tested claim names the reused pull request, selected evidence one commit
after the published baseline composed with it, family outcomes, carry-forward). A
lying adapter and an unknown key fail closed, and the mod's own checkout is never modified. Injected
kit defects (a same-head publication racing the rotation, a rotation missing the parent commit's
generation or ignoring the anchor grace) fail the in-process simulation. The canary's files are
checked as the canary procedure uses them: its caller is the managed template, every kit reference
is a placeholder pin that fills to one pin and every other action is a reviewed pin, ``template
init`` plus ``template check`` leave no drift, and its workflows keep least privilege; its adapter
carries a family generation forward only across an unchanged release matrix. Dependabot covers
every reviewed pin or names it as bumped by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import mod_base
from mod_base.config import load_config
from mod_base.adapter import host, host_child
from mod_base.adapter.api import Context
from mod_base.conformance import _fixture_api, _generations, _rotation, _simulation, _world
from mod_base.conformance import run as conformance_run
from mod_base.conformance._fixture_api import MAX_FIXTURE_ARCHIVE_BYTES, MAX_FIXTURE_READS, FixtureGitHub
from mod_base.conformance._snapshot import commit_file, make_snapshot, require_snapshot
from mod_base.conformance.run import _child_environment, run_conformance
from mod_base.errors import MbError
from mod_base.github.api import ReadOnlyViolation
from mod_base.github import artifacts as github_artifacts
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.pages import rotate, select
from mod_base.pin import parse_pin_files
from mod_base.template import tool
from tests import user_site
from tests.fixtures.mods import support
from tests.test_workflow_policy import (CHECKOUT, COMPOSITES, DEPLOY_PAGES, PINNED_ACTIONS, SETUP_PYTHON, parse_yaml,
                                        require_tools)

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "canary"
CANARY_WORKFLOWS = CANARY / ".github" / "workflows"
BANNER = "Synthetic demonstration evidence — not a product"
PLACEHOLDER_PIN = re.compile(r"^\s*(?:-\s+)?uses: The-Plum-Team/mod-base/(\S+)@\{\{PIN\}\} # \{\{VERSION\}\}$")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
PIN = "0123456789abcdef0123456789abcdef01234567"
VERSION = "v0.9.0"
#: A module-level guard: the adapter refuses to load in a process that holds a GitHub credential.
CREDENTIAL_GUARD = '''

import os as _credential_os

if any(_credential_os.environ.get(_name) for _name in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN")):
    raise RuntimeError("the conformance child holds a GitHub credential")
'''


def credential_guarded(root: Path) -> None:
    """The unmodified Quick Skin-like fixture (its adapter, family hook and optional conformance
    fixtures as shipped), whose adapter refuses to load in a process holding a credential."""

    adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
    adapter.write_text(adapter.read_text(encoding="utf-8") + CREDENTIAL_GUARD, encoding="utf-8")


def conformance(repo: Path, *, keys: tuple[str, ...] | None = None, families: bool = False) -> dict[str, Any]:
    return run_conformance(repo=repo, keys=keys, all_keys=keys is None, kit_root=ROOT, families=families)


def canary_copy(destination: Path) -> Path:
    """The canary as the canary procedure copies it (step 1), with its pins filled in (step 2)."""

    shutil.copytree(CANARY, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in sorted((destination / ".github" / "workflows").glob("*.yml")):
        path.write_text(path.read_text(encoding="utf-8").replace("{{PIN}}", PIN).replace("{{VERSION}}", VERSION),
                        encoding="utf-8")
    return destination


class CanaryConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = conformance(CANARY, families=True)

    def test_the_whole_generation_passes_with_every_family_outcome(self) -> None:
        report = self.report
        self.assertEqual(report["repository"], "The-Plum-Team/mod-base-canary")
        self.assertEqual([item["key"] for item in report["keys"]], ["mc1.20.1", "mc1.21.1"])
        self.assertEqual([item["anchor"] for item in report["keys"]], [True, False])
        self.assertTrue(all(item["scope"] == "complete" and item["frames"] > 0 for item in report["keys"]))
        self.assertEqual([(item["family"], item["status"]) for item in report["families"]],
                         [("demo-pairs", "available"), ("demo-pairs", "available")])
        self.assertEqual(report["variants"]["family-outcomes"], "passed")
        self.assertEqual(report["variants"]["newest-run"], "passed")
        self.assertEqual(report["variants"]["carried"], "passed")
        for skipped in ("attested", "delegated", "selected"):
            self.assertTrue(report["variants"][skipped].startswith("skipped: "), skipped)
        self.assertGreater(report["checks"], 50)

    def test_every_operation_is_admitted_as_configured(self) -> None:
        self.assertEqual(self.report["admission"], [
            "recovery:awaiting-complete-v1-evidence", "recovery:stale-implementation",
            "recovery:deferred-active-source", "manual:awaiting-complete-v1-evidence", "deploy:initial-ordinary",
            "recovery:initial-ordinary", "manual:manual", "family:initial-ordinary", "recovery:current",
            # the first generation's real rotation, the second head, the same-head family wake, the third head
            "recovery:current", "deploy:initial-ordinary", "recovery:current", "family:final-complete",
            "recovery:current", "deploy:initial-ordinary", "recovery:current"])

    def test_later_generations_carry_the_family_forward_and_rotate_for_real(self) -> None:
        generations = {item["generation"]: item for item in self.report["site"]["generations"]}
        self.assertEqual(sorted(generations), [2, 3, 4])
        # a new head: fresh handoffs, every family leg carried from the parent commit's cache
        self.assertEqual((generations[2]["head"], generations[2]["key_routes"], generations[2]["family_legs"]),
                         (2, {"handoff": 2}, {"carried": 2}))
        # the same head again (a family wake): the caches supersede the consumed handoffs, which the
        # interleaved rotation of generation 2 retires before this publication builds
        self.assertEqual((generations[3]["head"], generations[3]["key_routes"], generations[3]["family_legs"]),
                         (2, {"cache": 2}, {"carried": 1, "fresh": 1}))
        # a third head: both legs walk back to the second head's family caches
        self.assertEqual((generations[4]["head"], generations[4]["key_routes"], generations[4]["family_legs"]),
                         (3, {"handoff": 2}, {"carried": 2}))
        self.assertTrue(all(item["rotation_planned"] > 0 for item in generations.values()))
        self.assertLessEqual(self.report["site"]["max_job_reads"], 160)

    def test_every_refresh_after_a_sibling_upload_reads_its_inconsistent_listing_again(self) -> None:
        # The canary's own defect (run 36190041285): a refresh failed closed on a run artifact listing
        # whose total_count counted a sibling's upload before listing it. Each of the four
        # generations refreshes two keys and two family legs; every job after the first re-reads once.
        self.assertEqual(self.report["site"]["listing_rereads"], 4 * (2 + 2 - 1))

    def test_every_hook_the_canary_defines_ran(self) -> None:
        self.assertEqual(self.report["hooks"],
                         ["anchor_selection", "collect", "expectation", "family_validate", "targets"])
        self.assertEqual(self.report["kit"]["version"], mod_base.__version__)
        self.assertEqual(self.report["site"]["frames"], sum(item["frames"] for item in self.report["keys"]))
        self.assertGreater(self.report["site"]["rotation_planned"], 0)


class FixtureModConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-conformance-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_block_pops_like_authenticates_its_job_graph_and_attestation(self) -> None:
        report = conformance(support.MODS / "bp_like")
        self.assertEqual(report["variants"]["attested"], "passed")
        self.assertIn("expected_source_jobs", report["hooks"])
        self.assertEqual(report["families"], [])
        self.assertTrue(report["variants"]["carried"].startswith("skipped: "))
        # a second head is published and rotated; a family-less mod never republishes a current head
        self.assertEqual(report["admission"][-3:], ["deploy:always", "recovery:current", "manual:current"])
        self.assertEqual([(item["generation"], item["key_routes"]) for item in report["site"]["generations"]],
                         [(2, {"handoff": 1})])
        self.assertTrue(all(reason.split(":")[1] != "initial-ordinary" for reason in report["admission"]),
                        "admission mode 'always' never runs the progress policy")

    def test_quick_skin_like_passes_every_variant_without_credentials(self) -> None:
        mod = support.materialize("qs_like", self.work / "qs", mutate=credential_guarded)
        with mock.patch.dict(os.environ, {"GH_TOKEN": "parent-token", "GITHUB_TOKEN": "parent-token"}):
            report = conformance(mod.root, families=True)
        self.assertEqual({name: report["variants"][name] for name in ("delegated", "selected", "family-outcomes",
                                                                        "carried")},
                         {"delegated": "passed", "selected": "passed", "family-outcomes": "passed", "carried": "passed"})
        # The selection re-captures one session checkpoint: both session lanes mix epochs.
        self.assertEqual(report["site"]["composed_lanes"], {"baseline": 2, "mixed": 2, "selected": 0})
        self.assertEqual([(item["generation"], item["family_legs"]) for item in report["site"]["generations"]],
                         [(2, {"carried": 2}), (3, {"carried": 1, "fresh": 1}), (4, {"carried": 2})])
        self.assertEqual([(item["key"], item["status"]) for item in report["families"]],
                         [("mc1.20.1", "available"), ("mc26.3", "available")])
        self.assertIn("compose", report["hooks"])
        self.assertIn("authenticate_extensions", report["hooks"])
        self.assertEqual(support.git(mod.root, "status", "--porcelain", "--untracked-files=all"), "",
                         "conformance never modifies the mod's checkout")
        self.assertEqual(support.git(mod.root, "rev-parse", "HEAD"), mod.commit)

    def test_selected_keys_project_the_whole_generation(self) -> None:
        mod = support.materialize("qs_like", self.work / "qs")
        report = conformance(mod.root, keys=("mc26.3",))
        self.assertEqual([item["key"] for item in report["keys"]], ["mc26.3"])
        self.assertEqual([item["key"] for item in report["families"]], ["mc26.3"])
        with self.assertRaises(MbError) as caught:
            conformance(mod.root, keys=("mc9.9",))
        self.assertIn("declares no key mc9.9", str(caught.exception))

    def test_a_lying_collect_fails_closed(self) -> None:
        root = canary_copy(self.work / "canary")
        adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
        text = adapter.read_text(encoding="utf-8")
        lie = '"reported_pixel": role["metrics"][sorted(role["metrics"])[0]]'
        adapter.write_text(text.replace('"reported_pixel": role["metrics"][capture["step"]]', lie), encoding="utf-8")
        with self.assertRaises(MbError) as caught:
            conformance(root)
        self.assertIn("PixelMetrics", str(caught.exception))

    def test_arguments_are_checked_before_anything_runs(self) -> None:
        with self.assertRaises(MbError):
            run_conformance(repo=CANARY, keys=("mc1.20.1",), all_keys=True, kit_root=ROOT, families=False)
        with self.assertRaises(MbError):
            run_conformance(repo=CANARY, keys=None, all_keys=True, kit_root=self.work, families=False)
        with self.assertRaises(MbError):
            run_conformance(repo=self.work / "absent", keys=None, all_keys=True, kit_root=ROOT, families=False)

    def test_the_child_environment_holds_no_credential(self) -> None:
        # Every runner whose Pillow is not in its user site; ChildUserSiteTest covers the exception.
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x", "GITHUB_TOKEN": "y", "ACTIONS_RUNTIME_TOKEN": "z"}), \
                mock.patch.object(host, "imaging_user_site", return_value={}):
            environment = _child_environment("/kit/src", self.work)
        self.assertEqual(set(environment), {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONHASHSEED", "PYTHONSAFEPATH",
                                            "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONPATH"})
        self.assertEqual(environment["PYTHONPATH"], "/kit/src")
        self.assertTrue(Path(environment["HOME"]).is_relative_to(self.work))


#: The simulation child's variables before v1.0.2, in order.
DEFAULT_CHILD_NAMES = ("PATH", "HOME", "TMPDIR", "LANG", "PYTHONHASHSEED", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE",
                       "PYTHONNOUSERSITE", "PYTHONPATH")
#: A parent started with ``argv = [scratch, pythonpath, child source]``: it builds the simulation
#: child's environment exactly as ``run_conformance`` does and runs ``child source`` in it.
PARENT = """
import json, subprocess, sys
from pathlib import Path
from mod_base.adapter import host
from mod_base.conformance.run import _child_environment

helper = host.imaging_user_site()
located_only = "PIL" not in sys.modules
scratch = Path(sys.argv[1])
environment = _child_environment(sys.argv[2], scratch)
child = subprocess.run([sys.executable, "-P", "-c", sys.argv[3]], env=environment, cwd=scratch,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
print(json.dumps({"helper": helper, "located_only": located_only, "environment": environment,
                  "returncode": child.returncode, "stdout": child.stdout, "stderr": child.stderr[-2000:]}))
"""
#: The child: which PIL it imports and where its user site lies on ``sys.path``.
CHILD = """
import json, site, sys, sysconfig
try:
    import PIL
except ImportError:
    PIL = None
print(json.dumps({"marker": getattr(PIL, "MARKER", None), "file": getattr(PIL, "__file__", None),
                  "path": sys.path, "stdlib": sysconfig.get_paths()["stdlib"],
                  "user_site": site.getusersitepackages(), "enabled": site.ENABLE_USER_SITE}))
"""


class ChildUserSiteTest(unittest.TestCase):
    """v1.0.2: the simulation child's environment follows ``host.imaging_user_site`` and nothing
    else, and a child started that way really imports the Pillow of its parent's user site (Block
    Pops installs the hash-locked Pillow with ``pip install --user``; run 36239090095 failed with
    ``No module named 'PIL'`` in the ``synthesize`` hook)."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-conformance-user-site-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        (self.work / "layout").mkdir()
        self.layout = user_site.Layout(self.work / "layout")
        self.scratches = 0

    def child(self, scenario: user_site.Scenario | None) -> tuple[Path, dict[str, str]]:
        """``_child_environment`` in ``scenario`` (``None``: the helper answers ``{}``)."""

        self.scratches += 1
        scratch = self.work / f"scratch-{self.scratches}"
        scratch.mkdir()
        patch = (self.layout.patched(scenario) if scenario is not None
                 else mock.patch.object(host, "imaging_user_site", return_value={}))
        with patch:
            return scratch, _child_environment("/kit/src:/mod/scripts/pages", scratch)

    def test_every_other_process_state_keeps_the_environment_byte_for_byte(self) -> None:
        _, reference = self.child(None)
        self.assertEqual(tuple(reference), DEFAULT_CHILD_NAMES)
        for scenario in user_site.REJECTED:
            with self.subTest(scenario.label):
                scratch, environment = self.child(scenario)
                self.assertEqual(list(environment.items()), [
                    ("PATH", reference["PATH"]), ("HOME", str(scratch / "home")), ("TMPDIR", str(scratch)),
                    ("LANG", "C.UTF-8"), ("PYTHONHASHSEED", "0"), ("PYTHONSAFEPATH", "1"),
                    ("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONNOUSERSITE", "1"),
                    ("PYTHONPATH", "/kit/src:/mod/scripts/pages")])

    def test_a_user_site_pillow_swaps_nousersite_for_the_user_base(self) -> None:
        for scenario in user_site.ACCEPTED:
            with self.subTest(scenario.label):
                scratch, environment = self.child(scenario)
                _, reference = self.child(None)
                base = self.layout.expected(scenario)["PYTHONUSERBASE"]
                self.assertEqual(list(environment.items()), [
                    ("PATH", reference["PATH"]), ("HOME", str(scratch / "home")), ("TMPDIR", str(scratch)),
                    ("LANG", "C.UTF-8"), ("PYTHONHASHSEED", "0"), ("PYTHONSAFEPATH", "1"),
                    ("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONUSERBASE", base),
                    ("PYTHONPATH", "/kit/src:/mod/scripts/pages")])

    def parent(self, interpreter: str, base: Path, home: Path, **extra: str) -> dict[str, Any]:
        self.scratches += 1
        scratch = self.work / f"scratch-{self.scratches}"
        scratch.mkdir()
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONUSERBASE": str(base),
                       "PYTHONPATH": str(ROOT / "src"), "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1", **extra}
        completed = subprocess.run([interpreter, "-P", "-c", PARENT, str(scratch), str(ROOT / "src"), CHILD],
                                   env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=300, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        report = json.loads(completed.stdout)
        self.assertEqual(report["returncode"], 0, report["stderr"])
        report["child"] = json.loads(report["stdout"])
        return report

    def test_a_child_started_like_the_simulation_imports_the_parent_user_site(self) -> None:
        home = self.work / "home"
        home.mkdir()
        # A virtual environment without system site packages has no user site, so the parent may
        # be the interpreter that environment was created from; no platform lacks one.
        interpreter = user_site.user_site_interpreter(home)
        base = self.work / "userbase"
        base.mkdir()
        site_packages = user_site.user_site_of(interpreter, base, home)
        marker = "the parent's user-site Pillow"
        init = user_site.fake_pillow(site_packages, marker)

        report = self.parent(interpreter, base, home)
        self.assertEqual(report["helper"], {"PYTHONUSERBASE": str(base)})
        self.assertTrue(report["located_only"], "the helper must locate Pillow without importing it")
        environment, child = report["environment"], report["child"]
        self.assertNotIn("PYTHONNOUSERSITE", environment)
        self.assertEqual(environment["PYTHONUSERBASE"], str(base))
        self.assertEqual(environment["PYTHONPATH"], str(ROOT / "src"), "PYTHONPATH is never extended")
        self.assertNotEqual(environment["HOME"], str(home), "the child keeps its private HOME")
        self.assertEqual((child["marker"], os.path.realpath(child["file"])), (marker, os.path.realpath(init)))
        self.assertIs(child["enabled"], True)
        paths = [os.path.realpath(entry) for entry in child["path"]]
        self.assertIn(os.path.realpath(site_packages), paths)
        self.assertGreater(paths.index(os.path.realpath(site_packages)), paths.index(os.path.realpath(child["stdlib"])),
                           "the user site follows the standard library, as in the parent")
        self.assertLess(paths.index(os.path.realpath(ROOT / "src")), paths.index(os.path.realpath(child["stdlib"])))

        # The same parent without its user site: the child environment is the pre-v1.0.2 one.
        control = self.parent(interpreter, base, home, PYTHONNOUSERSITE="1")
        self.assertEqual(control["helper"], {})
        self.assertEqual(control["environment"]["PYTHONNOUSERSITE"], "1")
        self.assertNotIn("PYTHONUSERBASE", control["environment"])
        self.assertNotEqual(control["child"]["marker"], marker)


def simulate_in_process(source: Path, work: Path, *, families: bool,
                        keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    """The simulation in this process (so a test can alter the kit under it) over a snapshot of
    ``source`` (every key, or ``keys``)."""

    snapshot = work / "repo"
    config = load_config(source)
    make_snapshot(source, snapshot, branch=config.canonical_branch, replace=_world.pinned_workflows(config))
    scratch = work / "simulation"
    scratch.mkdir()
    return _simulation.simulate(_simulation.Settings(repo=snapshot, kit_root=ROOT, keys=keys, families=families,
                                                     work=scratch))


class ConformanceSensitivityTest(unittest.TestCase):
    """The later generations catch the cross-unit defects a single generation cannot: each kit
    defect below, injected in-process, fails the simulation at the stage that proves it."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-conformance-sensitivity-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_a_same_head_publication_that_races_the_rotation_fails(self) -> None:
        # Without supersession a same-head publication selects the handoff the previous generation
        # consumed; that generation's interleaved rotation retires it before this build.
        built = []
        original = _simulation.Simulation.build

        def build(simulation: Any, generation: Any, **arguments: Any) -> None:
            built.append(generation.number)
            original(simulation, generation, **arguments)

        with mock.patch.object(select, "supersedes", lambda owner, handoff: False), \
                mock.patch.object(_simulation.Simulation, "build", build):
            with self.assertRaises(MbError):
                simulate_in_process(CANARY, self.work, families=True)
        self.assertEqual(built, [1, 2, 3], "the same-head publication's build is the one that fails")

    def test_a_rotation_that_misses_the_parent_commits_generation_fails(self) -> None:
        with mock.patch.object(rotate._Rotation, "_window", lambda rotation, branch, coverage: [coverage]):
            with self.assertRaises(MbError) as caught:
                simulate_in_process(CANARY, self.work, families=True)
        self.assertIn("the second generation's dry-run rotation: rotation does not retire", str(caught.exception))

    def test_a_rotation_that_ignores_the_anchor_successor_grace_fails(self) -> None:
        original = rotate._Rotation._anchor_candidates

        def early(rotation: Any, keep: Any) -> Any:
            now = rotation.now
            rotation.now = now + rotate.timedelta(days=365)
            try:
                return original(rotation, keep)
            finally:
                rotation.now = now

        with mock.patch.object(rotate._Rotation, "_anchor_candidates", early):
            with self.assertRaises(MbError) as caught:
                simulate_in_process(support.MODS / "bp_like", self.work, families=False)
        self.assertIn("the second generation's dry-run rotation: rotation plans to retire", str(caught.exception))


class SimulationPartsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-conformance-parts-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_a_job_over_its_read_budget_or_changing_anything_fails(self) -> None:
        stub = mock.Mock(spec=["check", "report", "api"])
        stub.report = _simulation.Report()
        stub.check = lambda condition, message: _simulation.Simulation.check(stub, condition, message)
        api = FakeGitHub(repository="The-Plum-Team/example", default_branch="main", writable=True)
        with _simulation.Simulation.budget(stub, "within", api):
            for _ in range(160):
                api.get_json("/rate_limit")
        with self.assertRaises(MbError) as caught:
            with _simulation.Simulation.budget(stub, "over", api):
                for _ in range(161):
                    api.get_json("/rate_limit")
        self.assertIn("over made 161 API requests", str(caught.exception))
        api.add_artifact({"id": 7, "name": "mb-promotion", "created_at": "2026-09-01T12:00:00Z",
                          "workflow_run": {"id": 1, "head_branch": "main", "head_sha": "a" * 40}}, b"zip")
        with self.assertRaises(MbError) as caught:
            with _simulation.Simulation.budget(stub, "deleting", api):
                api.delete("/repos/The-Plum-Team/example/actions/artifacts/7")
        self.assertIn("changed the repository", str(caught.exception))

    def test_the_snapshot_commits_the_synthetic_pinned_workflows(self) -> None:
        # The simulated GitHub serves a pinned source and producer workflow at every head, and family
        # collect reads the producer's pin from the snapshot's own objects: both must agree.
        config = load_config(CANARY)
        replace = _world.pinned_workflows(config)
        self.assertEqual(set(replace), {config.source["workflow"],
                                        *(family["producer"]["workflow"] for family in config.families)})
        before = {path: (CANARY / path).read_bytes() for path in replace}
        snapshot = self.work / "repo"
        make_snapshot(CANARY, snapshot, branch=config.canonical_branch, replace=replace)
        for path, data in replace.items():
            with self.subTest(path=path):
                self.assertEqual(support.git(snapshot, "show", f"HEAD:{path}") + "\n", data.decode("utf-8"))
                pin = parse_pin_files({path: data})
                self.assertEqual((pin.sha, pin.version), (_world.KIT_SHA, "v" + mod_base.__version__))
        self.assertEqual({path: (CANARY / path).read_bytes() for path in replace}, before,
                         "the canary's own workflows are never modified")
        for label, bad in (("a traversal", {"../outside.yml": b"x"}), ("not bytes", {"a.yml": "x"}),
                           ("a directory", {"scripts": b"x"})):
            with self.subTest(label=label), self.assertRaises(MbError):
                make_snapshot(CANARY, self.work / f"bad-{label.replace(' ', '-')}", branch=config.canonical_branch,
                              replace=bad)  # type: ignore[arg-type]

    def test_a_later_head_is_one_documentation_commit_on_top(self) -> None:
        source = self.work / "mod"
        (source / "docs").mkdir(parents=True)
        (source / "docs" / "README.md").write_text("mod\n", encoding="utf-8")
        snapshot = self.work / "repo"
        first, _ = make_snapshot(source, snapshot, branch="main")
        head, tree = commit_file(snapshot, _generations.CONFORMANCE_DOCUMENT.format(number=2), b"two\n", branch="main")
        self.assertEqual(require_snapshot(snapshot, branch="main"), (head, tree))
        self.assertEqual(support.git(snapshot, "rev-parse", f"{head}^"), first)
        self.assertEqual(support.git(snapshot, "show", f"{head}:docs/mod-base-conformance/head-2.md"), "two")
        for relative in ("docs/mod-base-conformance/head-2.md", "../outside.md", "docs/../x.md"):
            with self.subTest(path=relative), self.assertRaises(MbError):
                commit_file(snapshot, relative, b"x", branch="main")
        (snapshot / "linked").symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(MbError):  # the snapshot is no longer clean
            commit_file(snapshot, "linked/x.md", b"x", branch="main")
        self.assertFalse((self.work / "x.md").exists())


    def test_the_selected_change_is_one_new_file_outside_github(self) -> None:
        def hooks(module: Any) -> Any:
            return mock.Mock(module=lambda invocation, field: module)

        self.assertEqual(_simulation._selected_change(hooks(object()), None), "docs/mod-base-conformance/selected.md")
        change = "modules/hud-preview/src/main/java/example/Feature.java"
        self.assertEqual(_simulation._selected_change(hooks(mock.Mock(SELECTED_CHANGE=change)), None), change)
        for bad in (".github/workflows/x.yml", ".GitHub/x", "../x.md", "a/.git/x", "/abs.md", "", None, 3):
            with self.subTest(change=bad), self.assertRaises(MbError):
                _simulation._selected_change(hooks(mock.Mock(SELECTED_CHANGE=bad)), None)


#: The Quick Skin-like adapter, and the lines after which a test adds its own baseline check.
QS_ADAPTER = Path("scripts") / "pages" / "mod_base_adapter.py"
COMPOSE_ANCHOR = '    reference = {"id": record["id"], "name": record["name"], "digest": record["digest"]}\n'
SELECTION_ANCHOR = "    if FEATURE_SELECTION in extensions:\n"
#: A ``compose`` that authenticates its baseline's owner itself, as Quick Skin's does: the upload of
#: a successful ``pages.yml`` run's refresh job, inside its retention step.
COMPOSE_OWNER_CHECK = '''    from mod_base.workflow import step_name
    owner = ctx.api.get_json(f"/repos/{ctx.api.repository}/actions/runs/{record['workflow_run']['id']}")
    if owner["path"] != ".github/workflows/pages.yml" or owner["conclusion"] != "success":
        raise ValueError("the baseline's owner is not a successful Pages run")
    runs = f"/repos/{ctx.api.repository}/actions/runs"
    jobs = ctx.api.get_json(f"{runs}/{owner['id']}/attempts/{owner['run_attempt']}/jobs")
    if not any(step["name"] == step_name("baseline_upload")
               and step["started_at"] <= record["created_at"] <= step["completed_at"]
               for job in jobs["jobs"] if job["name"] == api_job_name("finalize", "refresh", key=key)
               for step in job["steps"]):
        raise ValueError("the baseline was not uploaded in its Pages run's retention step")
'''
#: A ``compose`` that checks only that its baseline's owner ran at the commit the baseline names.
COMPOSE_COMMIT_CHECK = '''    if record["workflow_run"]["head_sha"] != record["name"].split("--")[2]:
        raise ValueError("the baseline's owner ran at another commit")
'''
#: An ``authenticate_extensions`` that refuses a selection whose baseline a source run uploaded.
SELECTION_OWNER_CHECK = '''        record = _artifact(ctx, extensions[FEATURE_SELECTION]["baseline_artifact_id"])
        owner = ctx.api.get_json(f"/repos/{ctx.api.repository}/actions/runs/{record['workflow_run']['id']}")
        if owner["path"] != ".github/workflows/pages.yml":
            raise ValueError("the selection's baseline is not a Pages run's upload")
'''


class SelectedAndDelegatedClaimTest(unittest.TestCase):
    """v1.0.1: the ``selected`` generation lies one commit after the newest published baseline's
    commit (a mod recomputes a selection as that Git diff), and a ``delegated`` handoff's tested
    claim names the tested pull-request run's own branch and commit. The Quick Skin-like adapter
    refuses a selection at its baseline's own commit, as Quick Skin's admission does. A forged
    baseline must be refused, by R3 or by the adapter's own ``compose``: a refusal by any other hook,
    or an accepted forgery, fails the run; each forgery differs from the genuine baseline only in its
    owner's workflow or upload window, never in its owner's commit."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-conformance-claims-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_the_selected_head_is_a_child_of_the_baseline_and_the_tested_claim_is_the_tested_runs(self) -> None:
        produced: dict[str, Any] = {}
        original = _simulation.Simulation.produce

        def produce(simulation: Any, world: Any, run: Any, key: str, **arguments: Any) -> Any:
            item = original(simulation, world, run, key, **arguments)
            produced[arguments["label"]] = (simulation, item)
            return item

        with mock.patch.object(_simulation.Simulation, "produce", produce):
            report = simulate_in_process(support.MODS / "qs_like", self.work, families=False)
        self.assertEqual((report["variants"]["delegated"], report["variants"]["selected"]), ("passed", "passed"))
        simulation, delegated = produced["mc1.20.1-delegated"]
        tested_run = simulation.world.runs[_simulation.DELEGATED_TESTED_RUN]
        self.assertEqual({field: delegated.manifest["provenance"]["tested"][field]
                          for field in ("run_id", "run_attempt", "branch", "commit")},
                         {"run_id": tested_run["id"], "run_attempt": 1, "branch": "conformance/reused-pull-request",
                          "commit": tested_run["head_sha"]})
        simulation, selected = produced["mc1.20.1-selected"]
        head = selected.manifest["subject"]["commit"]
        name = simulation.latest_baselines["mc1.20.1"]["name"]
        base = grammar.require_artifact_name(name, "baseline").commit
        snapshot = self.work / "repo"
        self.assertEqual(support.git(snapshot, "rev-parse", f"{head}^"), base)
        self.assertEqual(support.git(snapshot, "diff", "--name-only", base, head), "e2e/conformance-selected-change.md")
        self.assertEqual(support.git(snapshot, "rev-parse", "HEAD"), head)

    def test_a_selection_at_its_baselines_own_commit_is_refused(self) -> None:
        original = _generations.Generations.commit_on_head

        def at_the_baseline(simulation: Any, relative: str, data: bytes, *, label: str) -> None:
            if label != "the selected head":
                original(simulation, relative, data, label=label)

        with mock.patch.object(_simulation.Simulation, "commit_on_head", at_the_baseline):
            with self.assertRaises(MbError) as caught:
                simulate_in_process(support.MODS / "qs_like", self.work, families=False)
        self.assertIn("not a strict ancestor of the tested head", str(caught.exception))

    def qs_like_with(self, anchor: str, addition: str) -> Path:
        """A copy of the Quick Skin-like fixture whose adapter runs ``addition`` after ``anchor``."""

        source = self.work / "source"
        shutil.copytree(support.MODS / "qs_like", source, ignore=shutil.ignore_patterns("__pycache__"))
        adapter = source / QS_ADAPTER
        text = adapter.read_text(encoding="utf-8")
        self.assertEqual(text.count(anchor), 1)
        adapter.write_text(text.replace(anchor, anchor + addition), encoding="utf-8")
        return source

    def selected_refusals(self, source: Path) -> tuple[dict[str, Any], list[str]]:
        """The report of ``source``'s simulation (one key, no families) and the hooks the adapter
        refused (``HookFailed``) during its ``selected`` variant."""

        refused: list[str] = []
        original = _simulation.Simulation.selected

        def selected(simulation: Any) -> str | None:
            before = len(simulation.hooks.refusals)
            try:
                return original(simulation)
            finally:
                refused.extend(simulation.hooks.refusals[before:])

        with mock.patch.object(_simulation.Simulation, "selected", selected):
            report = simulate_in_process(source, self.work, families=False, keys=("mc1.20.1",))
        return report, refused

    def test_a_compose_that_refuses_both_forged_baselines_passes(self) -> None:
        report, refused = self.selected_refusals(self.qs_like_with(COMPOSE_ANCHOR, COMPOSE_OWNER_CHECK))
        self.assertEqual(report["variants"]["selected"], "passed")
        self.assertEqual(refused, ["compose", "compose"])

    def test_each_forged_baseline_shares_the_genuine_owners_commit(self) -> None:
        # A compose that checks only its owner's commit accepts both forgeries, so R3 refuses them.
        report, refused = self.selected_refusals(self.qs_like_with(COMPOSE_ANCHOR, COMPOSE_COMMIT_CHECK))
        self.assertEqual(report["variants"]["selected"], "passed")
        self.assertEqual(refused, [])

    def test_a_forged_baseline_refused_by_another_hook_fails(self) -> None:
        with self.assertRaises(MbError) as caught:
            self.selected_refusals(self.qs_like_with(SELECTION_ANCHOR, SELECTION_OWNER_CHECK))
        self.assertIn("the adapter's authenticate_extensions hook, not compose or R3, refused a source run's upload",
                      str(caught.exception))

    def test_an_accepted_forged_baseline_fails(self) -> None:
        def accept(invocation: Any, api: Any, *, key: str, baseline: Any) -> Any:
            return github_artifacts.get_artifact(api, baseline["id"])

        with mock.patch("mod_base.evidence.compose.authenticate_baseline", accept):
            with self.assertRaises(MbError) as caught:
                self.selected_refusals(support.MODS / "qs_like")
        self.assertIn("compose accepted a source run's upload as the baseline (R3)", str(caught.exception))


def zip_bytes(files: dict[str, bytes]) -> bytes:
    return _world.zip_files(files)


class FixtureApiTest(unittest.TestCase):
    """``ctx.api`` of the extension fixtures: the simulated GitHub read and seeded, typed and bounded."""

    REPOSITORY = "The-Plum-Team/qs-like"

    def setUp(self) -> None:
        self.config = load_config(support.MODS / "qs_like")
        self.world = _world.World(repository=self.REPOSITORY, config=self.config, head="a" * 40, tree="b" * 40)
        source = self.config.source["workflow"]
        self.tested = self.world.run(4400, path=source, event="pull_request", created=1800,
                                     head_branch="conformance/reused-pull-request")
        self.handoff = self.world.run(4401, path=source, event="workflow_dispatch", created=1900)
        protected = frozenset({source, *(family["producer"]["workflow"] for family in self.config.families)})
        self.api = FixtureGitHub(self.world, handoff_run=self.handoff, protected_workflows=protected)
        self.prefix = f"/repos/{self.REPOSITORY}"

    def test_an_artifact_carries_its_zip_bytes_and_returns_its_record(self) -> None:
        archive = zip_bytes({"tested-source.json": b"{}\n"})
        record = self.api.add_artifact(4400, "tested-source-e2e", archive)
        self.assertEqual((record["name"], record["size_in_bytes"], record["workflow_run"]["id"]),
                         ("tested-source-e2e", len(archive), 4400))
        self.assertEqual(record["digest"], "sha256:" + hashlib.sha256(archive).hexdigest())
        self.assertEqual(self.api.download(f"{self.prefix}/actions/artifacts/{record['id']}/zip",
                                           max_bytes=len(archive)), archive)
        listed = self.api.get_json(f"{self.prefix}/actions/runs/4400/artifacts", params={"name": "tested-source-e2e"})
        self.assertEqual([item["id"] for item in listed["artifacts"]], [record["id"]])
        self.assertEqual(self.api.handoff_run, {"id": 4401, "run_attempt": 1, "path": self.handoff["path"],
                                                "event": "workflow_dispatch", "head_branch": "master",
                                                "head_sha": "a" * 40})

    def test_jobs_stay_after_the_simulations_own_jobs_of_the_attempt(self) -> None:
        self.api.add_jobs(4401, 1, [{"name": "Reuse the tested source"}])
        self.world.jobs(self.handoff, [self.world.job("Prepare evidence", started=1950, completed=1990)])
        names = [job["name"] for job in self.api.get_json(f"{self.prefix}/actions/runs/4401/attempts/1/jobs")["jobs"]]
        self.assertEqual(names, ["Prepare evidence", "Reuse the tested source"])
        self.world.jobs(self.handoff, [self.world.job("Prepare evidence again", started=1950, completed=1990)])
        jobs = self.api.get_json(f"{self.prefix}/actions/runs/4401/attempts/1/jobs")["jobs"]
        self.assertEqual([job["name"] for job in jobs], ["Prepare evidence again", "Reuse the tested source"])
        self.assertEqual((jobs[1]["status"], jobs[1]["conclusion"], jobs[1]["run_id"]), ("completed", "success", 4401))
        # As GitHub's jobs API does, every job names its run's head (Quick Skin binds jobs to it).
        self.assertEqual({(job["head_sha"], job["head_branch"]) for job in jobs}, {("a" * 40, "master")})
        with self.assertRaisesRegex(ValueError, "repeats a job name"):
            self.api.add_jobs(4401, 1, [{"name": "Prepare evidence again"}])
        for bad in ([], [{"name": ""}], [{"name": "x", "id": 1}], [{"status": "completed"}]):
            with self.subTest(jobs=bad), self.assertRaises(ValueError):
                self.api.add_jobs(4401, 1, bad)
        for run_id, attempt in ((4402, 1), (4401, 2), (True, 1)):
            with self.subTest(run=run_id, attempt=attempt), self.assertRaises(ValueError):
                self.api.add_jobs(run_id, attempt, [{"name": "y"}])

    def test_a_run_of_another_workflow_is_assigned_its_identity(self) -> None:
        run = self.api.add_run({"path": ".github/workflows/feature-coverage.yml", "event": "workflow_run",
                                "head_branch": "master", "head_sha": "a" * 40, "display_title": "Coverage"})
        self.assertGreater(run["id"], 700_000)
        self.assertEqual((run["run_attempt"], run["status"], run["conclusion"], run["created_at"]),
                         (1, "completed", "success", self.handoff["created_at"]))
        self.assertEqual(self.api.get_json(f"{self.prefix}/actions/runs/{run['id']}")["display_title"], "Coverage")
        workflow = self.api.get_json(f"{self.prefix}/actions/workflows/feature-coverage.yml")
        self.assertEqual((workflow["id"], workflow["path"]), (run["workflow_id"], run["path"]))
        record = self.api.add_artifact(run["id"], "feature-coverage-certificate",
                                       zip_bytes({"certificate.json": b"{}"}))
        self.assertEqual(record["workflow_run"], {"id": run["id"], "head_branch": "master", "head_sha": "a" * 40})
        base = {"path": ".github/workflows/other.yml", "event": "push", "head_branch": "master", "head_sha": "a" * 40}
        for label, change in (("the source workflow", {"path": self.config.source["workflow"]}),
                               ("pages.yml", {"path": ".github/workflows/pages.yml"}),
                               ("a family producer", {"path": self.config.families[0]["producer"]["workflow"]}),
                               ("an assigned id", {"id": 1}), ("a foreign repository", {"repository": "x/y"}),
                               ("no commit", {"head_sha": "main"}), ("no workflow path", {"path": "x.yml"}),
                               ("a bad time", {"created_at": "yesterday"}), ("a bad status", {"status": "done"})):
            with self.subTest(label), self.assertRaises((ValueError, MbError)):
                self.api.add_run({**base, **change})

    def test_seeding_never_shadows_or_forges_what_the_simulation_owns(self) -> None:
        archive = zip_bytes({"a.json": b"{}"})
        for name in ("mb-handoff--mc1.20.1--a1", "mb-anything", "MB-x", "github-pages", "conformance-foreign",
                     "../x", "a b", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.api.add_artifact(4400, name, archive)
        for label, data in (("not bytes", "zip"), ("not a zip", b"PK\x03\x04 not really"), ("empty", b""),
                            ("oversized", b"0" * (MAX_FIXTURE_ARCHIVE_BYTES + 1))):
            with self.subTest(label), self.assertRaises(ValueError):
                self.api.add_artifact(4400, "x", data)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.api.add_artifact(9999, "x", archive)
        self.api.add_response(f"{self.prefix}/pulls/12", {"number": 12})
        self.assertEqual(self.api.get_json(f"{self.prefix}/pulls/12"), {"number": 12})
        for path in (f"{self.prefix}/actions/runs/4401", f"{self.prefix}/branches/master", f"{self.prefix}",
                     f"{self.prefix}/contents/site/mod-base.json", "/repos/x/y/pulls/1",
                     f"{self.prefix}/git/commits/{'a' * 40}", f"{self.prefix}/pulls/12"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.api.add_response(path, {})
        with self.assertRaises(ReadOnlyViolation):
            self.api.post_json(f"{self.prefix}/actions/workflows/x.yml/dispatches", {})
        with self.assertRaises(ReadOnlyViolation):
            self.api.delete(f"{self.prefix}/actions/artifacts/1")
        self.assertFalse(self.api.writable)

    def test_reads_are_bounded(self) -> None:
        for _ in range(MAX_FIXTURE_READS):
            self.api.get_json("/rate_limit")
        with self.assertRaisesRegex(ValueError, "at most"):
            self.api.get_json("/rate_limit")

    def test_retained_baselines_serve_only_selected_extensions_once_per_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "only selected_extensions"):
            self.api.retained_baseline("mc1.20.1")
        asked: list[str] = []

        def provide(key: str) -> dict[str, Any]:
            asked.append(key)
            if key == "mc9.9":
                raise ValueError("the adapter declares no key 'mc9.9'")
            return {"id": len(asked), "name": f"mb-baseline--{key}--{'a' * 40}--4242"}

        protected = frozenset({self.config.source["workflow"]})
        api = FixtureGitHub(self.world, handoff_run=self.handoff, protected_workflows=protected, baselines=provide)
        first = api.retained_baseline("mc26.3")
        first["id"] = 99
        self.assertEqual(api.retained_baseline("mc26.3"), {"id": 1, "name": f"mb-baseline--mc26.3--{'a' * 40}--4242"})
        self.assertEqual(asked, ["mc26.3"])
        for key in ("mc9.9", "not a key", 3):
            with self.subTest(key=key), self.assertRaises(ValueError):
                api.retained_baseline(key)  # type: ignore[arg-type]
        bounded = FixtureGitHub(self.world, handoff_run=self.handoff, protected_workflows=protected,
                                baselines=lambda key: {"key": key})
        for position in range(_fixture_api.MAX_FIXTURE_BASELINES):
            bounded.retained_baseline(f"mc1.{position}")
        with self.assertRaisesRegex(ValueError, "at most"):
            bounded.retained_baseline("mc2.0")


class ScratchTest(unittest.TestCase):
    """``run_conformance``'s scratch directory never replaces the simulation's own error."""

    def test_a_cleanup_failure_after_an_error_leaves_the_error(self) -> None:
        def failing_cleanup(path: Path) -> None:
            shutil.rmtree(path)
            raise OSError(16, "Device or resource busy")

        real_run = subprocess.run

        def child_fails(argv: Any, **options: Any) -> Any:
            if conformance_run.PROGRAM in argv:  # the simulation child; the snapshot's git calls run
                return subprocess.CompletedProcess(argv, 2, b"", b"mod_base.conformance.run: error: the real failure\n")
            return real_run(argv, **options)

        with mock.patch.object(conformance_run, "_remove_tree", failing_cleanup), \
                mock.patch.object(conformance_run.subprocess, "run", child_fails):
            with self.assertRaises(MbError) as caught:
                conformance(CANARY)
        self.assertIn("the simulation failed: error: the real failure", str(caught.exception))
        with mock.patch.object(conformance_run, "_remove_tree", failing_cleanup):
            with self.assertRaises(MbError) as caught:
                with conformance_run._scratch("mb-scratch-test-"):
                    pass
        self.assertIn("cannot remove the conformance scratch directory", str(caught.exception))

    def test_read_only_trees_are_removed(self) -> None:
        with conformance_run._scratch("mb-scratch-test-") as path:
            nested = path / "objects" / "ab"
            nested.mkdir(parents=True)
            (nested / "cd").write_bytes(b"x")
            os.chmod(nested / "cd", 0o400)
            os.chmod(nested, 0o500)
            os.chmod(path / "objects", 0o500)
            (path / "link").symlink_to(nested / "cd")
            kept = path
        self.assertFalse(os.path.lexists(kept))


class RotationOracleTest(unittest.TestCase):
    """The conformance oracle (``_rotation.expected_retirements``) on a small hand-built world."""

    HEAD, TREE = "a" * 40, "b" * 40

    def test_superseded_caches_consumed_handoffs_settled_anchors_and_transients(self) -> None:
        config = load_config(CANARY)
        world = _world.World(repository="The-Plum-Team/mod-base-canary", config=config, head=self.HEAD, tree=self.TREE)
        source, pages = config.source["workflow"], _simulation.PAGES_WORKFLOW_PATH
        first = world.run(1, path=source, event="workflow_dispatch", created=0)
        second = world.run(2, path=source, event="workflow_dispatch", created=800)
        earlier = world.run(10, path=pages, event="schedule", created=500, pages=True)
        failed = world.run(11, path=pages, event="schedule", created=600, conclusion="failure", pages=True)
        owner = world.run(12, path=pages, event="workflow_dispatch", created=1000, pages=True)
        key = "mc1.20.1"
        zipped = _world.zip_files({"x": b"x"})

        def upload(name: str, run: dict[str, Any], created: float) -> int:
            return world.artifact(name, run, created=created, archive=zipped)["id"]

        consumed = upload(grammar.handoff_name(key, 1), second, 1100 - 200)
        older_handoff = upload(grammar.handoff_name(key, 1), first, 300)
        superseded = upload(grammar.cache_name(key, self.HEAD), earlier, 550)
        upload(grammar.cache_name(key, self.HEAD), failed, 650)  # not a successful owner
        upload(grammar.cache_name(key, self.HEAD), owner, 1100)  # the replacement
        upload(grammar.baseline_name(key, self.HEAD, 1), earlier, 560)  # never retired
        upload("conformance-foreign-artifact", earlier, 570)  # never retired
        old_anchor = upload(grammar.anchor_name(key, self.HEAD, 1, 1), first, 310)
        upload(grammar.anchor_name(key, self.HEAD, 2, 1), second, 810)
        transients = {upload(grammar.collected_name(key), owner, 1050),
                      upload(grammar.PROMOTION_NAME, owner, 1150),
                      upload(grammar.PAGES_ARTIFACT_NAME, owner, 1150)}
        promotion = {"bundles": [{"key": key, "selected_artifact_id": consumed}], "families": []}

        def expected(now: float, alive: dict[int, Any] | None = None) -> set[int]:
            return _rotation.expected_retirements(runs=world.runs, alive=world.alive() if alive is None else alive,
                                                  records=world.records, config=config, owner_run_id=owner["id"],
                                                  promotion=promotion, anchored={key}, family_producers={},
                                                  now=_world.timestamp(now))

        common = {consumed, older_handoff, superseded} | transients
        # the second anchor settles only once its run and upload lie before now - grace (0 days here)
        self.assertEqual(expected(2000), common | {old_anchor})
        self.assertEqual(expected(700), common)
        # a handoff not older than the selected artifact's run is kept, the consumed one never is
        newer = upload(grammar.handoff_name(key, 1), world.run(3, path=source, event="workflow_dispatch",
                                                                        created=900), 950)
        self.assertNotIn(newer, expected(2000))


class CanaryCarryForwardTest(unittest.TestCase):
    """The canary adapter's carry-forward decision (``family_validate``) on real commits: the
    envelope's generation is carried to a descendant with an unchanged release matrix, refused after
    a matrix change and without ``carry_forward``. The kit's own R4/R5 are covered by conformance."""

    FAMILY, KEY = "demo-pairs", "mc1.20.1"

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-canary-carry-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = canary_copy(self.work / "canary")
        support.git(self.root, "init", "-q", "--initial-branch=main")
        self.first = self.commit({})
        self.docs = self.commit({"docs/note.md": "documentation only\n"})
        matrix = (self.root / "release" / "release-matrix.json").read_text(encoding="utf-8")
        self.matrix = self.commit({"release/release-matrix.json": matrix + "\n"})
        self.adapter = host_child.load_adapter(self.root / "scripts" / "pages" / "mod_base_adapter.py")
        self.bundle = self.work / "bundle"
        self.bundle.mkdir()
        contract = (self.root / "e2e" / "scenario-contract.json").read_bytes()  # unchanged by every commit
        producer = {"run_id": 88, "run_attempt": 1, "commit": self.first}
        (self.bundle / "envelope.json").write_text(json.dumps(
            {"coverage_sha": self.first, "subject": {"branch": "main", "commit": self.first}, "producer": producer}),
            encoding="utf-8")
        (self.bundle / "manifest.json").write_text(json.dumps(
            {"kind": "mod-base-canary.pairs", "schema_version": 1, "family": self.FAMILY, "key": self.KEY,
             "coverage_sha": self.first, "contract_sha256": hashlib.sha256(contract).hexdigest(),
             "producer": producer, "lanes": [], "not_applicable": []}), encoding="utf-8")

    def commit(self, files: dict[str, str]) -> str:
        for relative, text in files.items():
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_text(text, encoding="utf-8")
        support.git(self.root, "add", "-A")
        support.git(self.root, "commit", "-q", "-m", "canary")
        return support.git(self.root, "rev-parse", "HEAD")

    def validate(self, expected: str) -> dict[str, Any]:
        output = Path(tempfile.mkdtemp(dir=self.work))
        context = Context(repo_root=self.root, config=load_config(self.root), tmpdir=Path(tempfile.mkdtemp(dir=self.work)),
                          implementation_sha=expected)
        return self.adapter.family_validate(context, self.FAMILY, self.KEY, str(self.bundle), expected, str(output))

    def test_an_unchanged_matrix_carries_the_generation_forward(self) -> None:
        self.assertNotIn("carried_from", self.validate(self.first))
        result = self.validate(self.docs)
        self.assertEqual((result["status"], result["carried_from"]), ("available", self.first))

    def test_a_matrix_change_or_a_family_without_carry_forward_refuses(self) -> None:
        self.assertEqual(self.validate(self.matrix)["status"], "unavailable")
        path = self.root / "site" / "mod-base.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"carry_forward": true', '"carry_forward": false'),
                        encoding="utf-8")
        result = self.validate(self.docs)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("no carry-forward", result["reason"])


class CanaryFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-canary-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_the_canary_caller_is_the_managed_template(self) -> None:
        self.assertEqual((CANARY_WORKFLOWS / "pages.yml").read_bytes(),
                         (ROOT / "template" / "managed" / ".github" / "workflows" / "pages.yml").read_bytes())

    def test_every_kit_reference_is_a_placeholder_pin(self) -> None:
        files = {}
        for path in sorted(CANARY_WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if "uses:" in line and "mod-base" in line.lower():
                    with self.subTest(file=path.name, line=number):
                        self.assertRegex(line, PLACEHOLDER_PIN)
            files[f".github/workflows/{path.name}"] = text.replace("{{PIN}}", PIN).replace("{{VERSION}}",
                                                                                          VERSION).encode("utf-8")
        pin = parse_pin_files(files)
        self.assertEqual((pin.sha, pin.version), (PIN, VERSION))
        used = {reference.split("@")[0] for reference in pin.references}
        self.assertEqual(used, {f".github/workflows/{name}.yml" for name in ("pages", "canary-producer", "canary-family")},
                         "the probe observes only; every other canary workflow pins the kit")

    def test_every_other_action_is_a_reviewed_pin(self) -> None:
        # Dependabot never scans canary/ (.github/dependabot.yml): a reviewed pin bump must reach it.
        seen = set()
        for path in sorted(CANARY_WORKFLOWS.glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = USES.match(line)
                if match is None or PLACEHOLDER_PIN.match(line):
                    continue
                action, _, comment = match.group(1).partition(" # ")
                with self.subTest(file=path.name, line=number):
                    self.assertIn(action, PINNED_ACTIONS)
                    self.assertEqual(comment, PINNED_ACTIONS[action])
                seen.add(action)
        self.assertEqual(seen, {CHECKOUT, SETUP_PYTHON, DEPLOY_PAGES})

    def test_the_canary_procedure_seeds_a_repository_without_drift(self) -> None:
        root = canary_copy(self.work / "mod-base-canary")
        created = tool.init(root, kit_root=ROOT, seed=True, from_config=root / "site" / "mod-base.json")
        self.assertIn("scripts/ci/mod_base_kit.py", created)
        self.assertNotIn("LICENSE", created, "the canary carries its own license")
        self.assertEqual(tool.check(root, kit_root=ROOT), [])
        load_config(root)

    def test_every_published_text_says_synthetic(self) -> None:
        config = load_config(CANARY)
        self.assertIn("synthetic", config.project["name"], "the name is on every page, the gallery's included")
        for text in (config.project["tagline"], config.copy["gallery_lead"], config.copy["evidence_lead"],
                     config.copy["principles"][0], config.copy["methodology"][0]):
            self.assertIn(BANNER, text)
        self.assertIn(BANNER, (CANARY / "release" / "release-matrix.json").read_text(encoding="utf-8"))
        for path in [CANARY / "README.md", *sorted(CANARY_WORKFLOWS.glob("canary-*.yml"))]:
            with self.subTest(file=path.name):
                self.assertIn(BANNER, path.read_text(encoding="utf-8"))

    def test_the_canary_workflows_keep_least_privilege(self) -> None:
        for path in sorted(CANARY_WORKFLOWS.glob("canary-*.yml")):
            document = parse_yaml(path.read_text(encoding="utf-8"), path.name)
            self.assertEqual(document["permissions"], {}, path.name)
            for job_id, job in document["jobs"].items():
                with self.subTest(file=path.name, job=job_id):
                    permissions = job["permissions"]
                    steps = job["steps"]
                    if job_id.startswith("notify"):
                        self.assertEqual(permissions, {"actions": "write"})
                        self.assertFalse(any(str(item.get("uses", "")).startswith("actions/checkout@")
                                             for item in steps))
                    else:
                        self.assertTrue(set(permissions.values()) <= {"read"}, permissions)
                    for item in steps:
                        if str(item.get("uses", "")).startswith("actions/checkout@"):
                            self.assertEqual(item["with"]["persist-credentials"], "false")
                        self.assertNotIn("${{", item.get("run", ""))

    def test_the_canary_workflows_pass_actionlint(self) -> None:
        require_tools("actionlint", "shellcheck")
        workflows = self.work / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for path in sorted(CANARY_WORKFLOWS.glob("*.yml")):
            (workflows / path.name).write_text(path.read_text(encoding="utf-8").replace("{{PIN}}", PIN)
                                               .replace("{{VERSION}}", VERSION), encoding="utf-8")
        files = sorted(str(path.relative_to(self.work)) for path in workflows.iterdir())
        completed = subprocess.run([shutil.which("actionlint") or "actionlint", "-no-color", *files], cwd=self.work,
                                   capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class CanaryProducerTest(unittest.TestCase):
    """``scripts/canary/produce.py`` as the canary's producer workflows run it, handed to the kit's
    own ``prepare``, ``validate`` and ``family envelope`` commands (the isolated adapter host)."""

    REPOSITORY = "The-Plum-Team/mod-base-canary"

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-canary-producer-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = canary_copy(self.work / "canary")
        support.git(self.root, "init", "-q", "--initial-branch=main")
        support.git(self.root, "add", "-A")
        support.git(self.root, "commit", "-q", "-m", "canary")
        self.head = support.git(self.root, "rev-parse", "HEAD")
        self.tree = support.git(self.root, "rev-parse", "HEAD^{tree}")

    def environment(self, workflow: str, run_id: int) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.work), "LANG": "C.UTF-8",
                "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1",
                "GITHUB_REPOSITORY": self.REPOSITORY, "GITHUB_SHA": self.head, "GITHUB_RUN_ID": str(run_id),
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
                "GITHUB_WORKFLOW_REF": f"{self.REPOSITORY}/.github/workflows/{workflow}@refs/heads/main",
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_JOB": "evidence", "MOD_BASE_KIT_SHA": PIN}

    def run_tool(self, environment: dict[str, str], *arguments: str) -> str:
        completed = subprocess.run([sys.executable, "-P", *arguments], cwd=self.root, env=environment,
                                   capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
        return completed.stdout

    def test_the_producer_hands_off_valid_evidence_through_the_kit(self) -> None:
        environment = self.environment("canary-producer.yml", 77)
        keys = json.loads(self.run_tool(environment, "scripts/canary/produce.py", "keys"))
        self.assertEqual(keys, ["mc1.20.1", "mc1.21.1"])
        e2e = self.work / "e2e"
        self.run_tool(environment, "scripts/canary/produce.py", "synthesize", "--key", keys[0], "--output", str(e2e))
        output = self.work / "github-output"
        output.touch()
        handoff = self.work / "handoff"
        self.run_tool({**environment, "GITHUB_OUTPUT": str(output)}, "-m", "mod_base", "prepare", "--repo", ".",
                      "--e2e-root", str(e2e), "--key", keys[0], "--output", str(handoff),
                      "--subject-branch", "main", "--subject-commit", self.head, "--subject-tree", self.tree,
                      "--tested-run-id", "77", "--tested-run-attempt", "1", "--tested-branch", "main",
                      "--tested-commit", self.head, "--tested-controller-branch", "main",
                      "--tested-controller-sha", self.head, "--anchor", "auto")
        self.assertIn("anchor_eligible=true", output.read_text(encoding="utf-8"))
        self.run_tool(environment, "-m", "mod_base", "validate", "--repo", ".", "--key", keys[0], "--kind", "handoff",
                      "--input", str(handoff), "--expected-subject-commit", self.head)
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["repository"], self.REPOSITORY)
        self.assertEqual(manifest["provenance"]["reuse"], "none")
        self.assertTrue(manifest["frames"])
        self.assertTrue(all(frame["runtime_evidence"].endswith("(not a product)") for frame in manifest["frames"]))

    def test_the_family_producer_writes_an_envelope_the_kit_accepts(self) -> None:
        environment = self.environment("canary-family.yml", 88)
        run = {"id": 88, "run_attempt": 1, "head_sha": self.head, "event": "workflow_dispatch",
               "created_at": "2026-09-01T12:00:00Z", "display_title": "Canary family", "status": "in_progress"}
        run_json = self.work / "run.json"
        run_json.write_text(json.dumps(run), encoding="utf-8")
        native = self.work / "native"
        self.run_tool(environment, "scripts/canary/produce.py", "family", "--key", "mc1.20.1", "--run-json",
                      str(run_json), "--output", str(native))
        envelope = self.work / "envelope"
        self.run_tool(environment, "-m", "mod_base", "family", "envelope", "--repo", ".", "--family", "demo-pairs",
                      "--key", "mc1.20.1", "--bundle", str(native), "--coverage-sha", self.head,
                      "--subject-branch", "main", "--subject-commit", self.head, "--output", str(envelope))
        self.run_tool(environment, "-m", "mod_base", "validate", "--repo", ".", "--key", "mc1.20.1",
                      "--kind", "family", "--input", str(envelope))
        written = json.loads((native / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(written["producer"]["event"], "workflow_dispatch")
        self.assertEqual(written["producer"]["run_id"], 88)
        self.assertEqual(written["coverage_sha"], self.head)


class DependabotTest(unittest.TestCase):
    """``.github/dependabot.yml``: every reviewed pin is either proposed by Dependabot or named as
    bumped by hand (with ``PINNED_ACTIONS``, in the same pull request)."""

    def test_every_reviewed_pin_is_scanned_or_bumped_by_hand(self) -> None:
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        (update,) = parse_yaml(text, "dependabot.yml")["updates"]
        self.assertEqual(update["package-ecosystem"], "github-actions")
        directories = update["directories"]
        self.assertEqual(directories, ["/", *(f"/actions/{name}" for name in COMPOSITES)])
        scanned = set()
        for directory in directories:
            # "/" means .github/workflows plus a root action.yml; any other directory, its own YAML files.
            root = ROOT / ".github" / "workflows" if directory == "/" else ROOT / directory.lstrip("/")
            paths = sorted(root.glob("*.yml")) + ([ROOT / "action.yml"] if directory == "/" else [])
            for path in paths:
                if path.is_file():
                    scanned |= {match.group(1).partition(" # ")[0]
                                for match in map(USES.match, path.read_text(encoding="utf-8").splitlines()) if match}
        by_hand = sorted(action.split("@")[0] for action in set(PINNED_ACTIONS) - scanned)
        self.assertEqual(by_hand, ["actions/create-github-app-token", "actions/deploy-pages"])
        for action in by_hand:
            self.assertIn(f"only user of {action}", text)
        self.assertIn("template/managed/.github/workflows/pages.yml", text)
        self.assertIn("template/managed/.github/workflows/mod-base-gate-status.yml", text)
        self.assertIn("canary/.github/workflows/canary-*.yml", text)


class KitCiWorkflowTest(unittest.TestCase):
    """``.github/workflows/ci.yml`` (SPEC §5.10): the required contexts, least privilege and pins."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        cls.document = parse_yaml(cls.text, "ci.yml")

    def test_the_required_contexts_and_triggers(self) -> None:
        document = self.document
        self.assertEqual(document["name"], "mod-base CI")
        self.assertEqual(document["on"], {"pull_request": {}, "push": {"branches": ["main"]}})
        names = {job["name"] for job in document["jobs"].values()}
        self.assertTrue({"Test", "Workflow policy", "Front end", "Conformance (informational)"} <= names)
        self.assertEqual(document["jobs"]["test"]["strategy"]["matrix"]["python"], ["3.11", "3.12", "3.13"])
        self.assertEqual(document["jobs"]["test-gate"]["needs"],
                         ["test", "hosted-worker", "hosted-deferred", "hosted-pipeline"])
        self.assertEqual(document["jobs"]["conformance"]["continue-on-error"], "true")

    def test_account_modules_have_separate_required_python_matrices(self) -> None:
        jobs = self.document["jobs"]
        self.assertEqual({job_id: jobs[job_id]["timeout-minutes"] for job_id in
                          ("test", "hosted-worker", "hosted-deferred", "hosted-pipeline")},
                         {"test": "30", "hosted-worker": "60", "hosted-deferred": "15", "hosted-pipeline": "30"})
        for job_id, module in (("hosted-worker", "ci_linux_worker"),
                               ("hosted-deferred", "ci_linux_deferred"),
                               ("hosted-pipeline", "ci_linux_pipeline")):
            with self.subTest(job=job_id):
                job = jobs[job_id]
                self.assertEqual(job["strategy"]["matrix"]["python"], ["3.11", "3.12", "3.13"])
                self.assertNotIn("continue-on-error", job)
                self.assertIn(f"python3 -m unittest -v tests.{module}", job["steps"][-1]["run"])
                self.assertTrue((ROOT / "tests" / f"{module}.py").is_file())
                self.assertIn(f"needs.{job_id}.result", str(jobs["test-gate"]["steps"]))
        self.assertNotIn("tests.ci_linux_", str(jobs["test"]["steps"]))

    def test_every_job_reads_only_and_every_action_is_pinned(self) -> None:
        self.assertEqual(self.document["permissions"], {})
        for job_id, job in self.document["jobs"].items():
            with self.subTest(job=job_id):
                self.assertIn(job["permissions"], ({"contents": "read"}, {}))
                for item in job["steps"]:
                    self.assertNotIn("${{", item.get("run", ""))
                    if "uses" not in item:
                        continue
                    self.assertIn(item["uses"], (CHECKOUT, SETUP_PYTHON))
                    if item["uses"] == CHECKOUT:
                        self.assertEqual(item["with"]["persist-credentials"], "false")
        for line in self.text.splitlines():
            if line.strip().startswith(("uses:", "- uses:")):
                action, _, comment = line.split("uses:", 1)[1].strip().partition(" # ")
                with self.subTest(uses=action):
                    self.assertEqual(comment, PINNED_ACTIONS[action])

    def test_actionlint_is_the_pinned_release(self) -> None:
        digest = (ROOT / "tools" / "actionlint.sha256").read_text(encoding="utf-8")
        self.assertRegex(digest, r"^[0-9a-f]{64}  actionlint_\d+\.\d+\.\d+_linux_amd64\.tar\.gz\n$")
        installs = [item["run"] for job in self.document["jobs"].values() for item in job["steps"]
                    if item.get("name") == "Install the pinned actionlint and check the shell tools"]
        self.assertEqual(len(installs), 2, "the Test matrix and Workflow policy both install actionlint")
        for script in installs:
            self.assertIn('sha256sum --check --strict "$GITHUB_WORKSPACE/tools/actionlint.sha256"', script)
            self.assertIn("--proto '=https'", script)

    def test_conformance_reads_both_mods_without_credentials(self) -> None:
        steps = self.document["jobs"]["conformance"]["steps"]
        checkouts = {item["with"].get("repository"): item["with"] for item in steps
                     if str(item.get("uses", "")).startswith("actions/checkout@") and "with" in item}
        for repository in ("The-Plum-Team/Quick-Skin-Mod", "The-Plum-Team/Block-Pops-Minecraft-Mod"):
            with self.subTest(repository=repository):
                self.assertEqual(checkouts[repository]["ref"], "master")
                self.assertEqual(checkouts[repository]["persist-credentials"], "false")
        script = steps[-1]["run"]
        self.assertIn('if [[ ! -f "$mod/site/mod-base.json" ]]', script)
        self.assertIn("python3 -P -m mod_base conformance --repo \"$mod\" --kit-root . --all --families", script)


if __name__ == "__main__":
    unittest.main()
