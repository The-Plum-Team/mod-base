"""The lossless anchor (SPEC §3.4): eligibility (adapter selection plus the direct-run rule),
creation from an uploaded handoff, canonical PNGs, raw-artifact binding and the grace data the
rotation applies (Quick Skin 0 days, Block Pops 8 days)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.config import load_config
from mod_base.errors import MbError
from mod_base.evidence import anchor, prepare, validate
from mod_base.imaging.metrics import SizePolicy, inspect_png
from mod_base.imaging.png import canonical_png
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import validate_anchor
from mod_base.runtime import build_invocation
from tests.fixtures.mods import support

QS_KEY = "mc1.20.1"
BP_KEY = hashlib.sha256(b"master").hexdigest()[:24]
DIGEST = "sha256:" + "d" * 64
KIT = Path(__file__).resolve().parents[1]


class AnchorFlow(unittest.TestCase):
    fixture = "qs_like"

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-anchor-test-")).resolve()
        cls.mod = support.materialize(cls.fixture, cls.directory / "repo")
        cls.env = support.environment(cls.mod, run_id=6100, attempt=2)
        cls.stub = support.InProcessHost()
        with mock.patch.object(host, "call", cls.stub):
            cls.invocation = support.invocation(cls.mod, cls.env, implementation_sha=cls.mod.commit)
            cls.handoffs: dict[str, Path] = {}
            keys = (QS_KEY, "mc26.3") if cls.fixture == "qs_like" else (BP_KEY,)
            for key in keys:
                e2e = cls.directory / f"e2e-{key}"
                support.synthesize(cls.invocation, key, cls.mod.subject, e2e)
                handoff, tested = support.claims(cls.env, subject=cls.mod.subject)
                prepare.prepare_handoff(cls.invocation, e2e_root=e2e, key=key, output=cls.directory / key,
                                        subject=cls.mod.subject, tested=tested, handoff=handoff)
                cls.handoffs[key] = cls.directory / key

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-anchor-case-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.stub = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, key: str, output: str = "anchor", **options: Any) -> dict[str, Any]:
        arguments = {"raw_artifact_id": 31, "raw_artifact_name": grammar.handoff_name(key, 2),
                     "raw_artifact_digest": DIGEST, **options}
        return anchor.create_anchor(self.invocation, key=key, handoff_dir=self.handoffs[key],
                                    output=self.work / output, **arguments)

    def prepare(self, key: str, output: str, *, environ: dict[str, str] | None = None,
                tested: dict[str, Any] | None = None, **options: Any) -> prepare.PrepareResult:
        """Another handoff of ``key`` from the shared E2E output (``anchor`` auto)."""

        environ = environ or self.env
        handoff, direct = support.claims(environ, subject=self.mod.subject)
        return prepare.prepare_handoff(support.invocation(self.mod, environ), e2e_root=self.directory / f"e2e-{key}",
                                       key=key, output=self.work / output, subject=self.mod.subject,
                                       tested=tested or direct, handoff=handoff, anchor="auto", **options)

    def forked(self, change) -> support.FixtureMod:
        """A copy of the fixture repository whose working-tree adapter is ``change(source)``."""

        root = self.work / "forked-repo"
        shutil.copytree(self.mod.root, root, symlinks=True)
        adapter = root / "scripts/pages/mod_base_adapter.py"
        source = adapter.read_text(encoding="utf-8")
        self.assertNotEqual(change(source), source, "the fork must change the adapter")
        adapter.write_text(change(source), encoding="utf-8")
        return support.FixtureMod(name=self.mod.name, root=root, commit=self.mod.commit, tree=self.mod.tree,
                                  branch=self.mod.branch, repository=self.mod.repository)

    def edit_anchor(self, change) -> Path:
        root = self.work / "anchor"
        manifest = json.loads((root / "manifest.json").read_bytes())
        change(manifest)
        validate_anchor(manifest)
        (root / "manifest.json").write_bytes(canonical_json(manifest))
        return root


class QuickSkinAnchorTest(AnchorFlow):
    def test_identity_of_an_eligible_direct_run(self) -> None:
        identity = anchor.anchor_identity(self.invocation, self.handoffs[QS_KEY], key=QS_KEY)
        self.assertTrue(identity.eligible)
        self.assertEqual(identity.name, f"mb-anchor--{QS_KEY}--{self.mod.commit}--6100--a2")
        self.assertEqual(identity.name, anchor.artifact_name(QS_KEY, self.mod.commit, 6100, 2))
        self.assertEqual(identity.artifact_nodes, ("fabric-1.20.1", "forge-1.20.1"))
        self.assertEqual(grammar.parse_artifact_name(identity.name).kind, "anchor")

    def test_the_adapter_may_decline(self) -> None:
        identity = anchor.anchor_identity(self.invocation, self.handoffs["mc26.3"], key="mc26.3")
        self.assertEqual(identity, anchor.AnchorIdentity(eligible=False, name=None, artifact_nodes=()))
        with self.assertRaises(MbError) as caught:
            self.create("mc26.3")
        self.assertEqual(caught.exception.reason, "anchor-ineligible")

    def test_a_disabled_anchor_never_asks_the_adapter(self) -> None:
        config = self.work / "config.json"
        data = json.loads((self.mod.root / "site/mod-base.json").read_text())
        data["anchor"]["enabled"] = False
        config.write_text(json.dumps(data))
        disabled = build_invocation(self.mod.root, config, self.env)
        identity = anchor.anchor_identity(disabled, self.handoffs[QS_KEY], key=QS_KEY)
        self.assertFalse(identity.eligible)
        self.assertNotIn(("anchor_selection", False), self.stub.calls)

    def test_only_a_direct_canonical_run_is_eligible(self) -> None:
        controller = "c" * 40
        environ = support.environment(self.mod, run_id=6200, sha=controller)
        handoff, tested = support.claims(environ, subject=self.mod.subject)
        self.assertEqual(handoff["controller_sha"], controller)
        e2e = self.work / "e2e"
        support.synthesize(self.invocation, QS_KEY, self.mod.subject, e2e)
        result = prepare.prepare_handoff(support.invocation(self.mod, environ, implementation_sha=self.mod.commit),
                                         e2e_root=e2e, key=QS_KEY, output=self.work / "split",
                                         subject=self.mod.subject, tested=tested, handoff=handoff)
        self.assertFalse(result.anchor_eligible)
        identity = anchor.anchor_identity(self.invocation, self.work / "split", key=QS_KEY)
        self.assertFalse(identity.eligible)
        with self.assertRaisesRegex(MbError, "not an eligible anchor source"):
            anchor.create_anchor(self.invocation, key=QS_KEY, handoff_dir=self.work / "split", raw_artifact_id=31,
                                 raw_artifact_name=grammar.handoff_name(QS_KEY, 1), raw_artifact_digest=DIGEST,
                                 output=self.work / "anchor")

    def test_a_handoff_run_on_another_branch_is_not_a_direct_run_of_the_subject(self) -> None:
        # controller_sha == subject.commit alone is not BP's rule: the run must also have run on
        # the subject's branch.
        environ = support.environment(self.mod, run_id=6300, branch="release")
        result = self.prepare(QS_KEY, "split", environ=environ)
        self.assertEqual(result.manifest["provenance"]["handoff"]["controller_sha"], self.mod.commit)
        self.assertFalse(result.anchor_eligible)
        self.assertFalse(anchor.anchor_identity(self.invocation, self.work / "split", key=QS_KEY).eligible)
        self.assertNotIn(("anchor_selection", False), self.stub.calls)

    def test_delegated_reuse_of_an_identical_tree_stays_eligible(self) -> None:
        extensions = self.work / "extensions.json"
        extensions.write_text(json.dumps({"quick-skin.runtime_source": {
            "repository": self.mod.repository, "run_id": 4000, "tested_sha": "e" * 40}}))
        handoff, direct = support.claims(self.env, subject=self.mod.subject)
        tested = {**direct, "run_id": 4000, "commit": "e" * 40, "controller_sha": "e" * 40}
        result = self.prepare(QS_KEY, "delegated", tested=tested, extensions_path=extensions)
        self.assertEqual(result.manifest["provenance"]["reuse"], "delegated")
        self.assertTrue(result.anchor_eligible)
        manifest = anchor.create_anchor(self.invocation, key=QS_KEY, handoff_dir=self.work / "delegated",
                                        raw_artifact_id=31, raw_artifact_name=grammar.handoff_name(QS_KEY, 2),
                                        raw_artifact_digest=DIGEST, output=self.work / "anchor")
        self.assertEqual(manifest["provenance"]["tested"], tested, "the anchor records where its pixels came from")

    def test_an_adapter_without_anchor_selection_declines_every_anchor(self) -> None:
        without = self.forked(lambda source: source.replace("def anchor_selection(", "def _anchor_selection("))
        declining = support.invocation(without, self.env, implementation_sha=self.mod.commit)
        self.assertFalse(anchor.anchor_identity(declining, self.handoffs[QS_KEY], key=QS_KEY).eligible)
        with self.assertRaises(MbError) as caught:
            anchor.create_anchor(declining, key=QS_KEY, handoff_dir=self.handoffs[QS_KEY], raw_artifact_id=31,
                                 raw_artifact_name=grammar.handoff_name(QS_KEY, 2), raw_artifact_digest=DIGEST,
                                 output=self.work / "anchor")
        self.assertEqual(caught.exception.reason, "anchor-ineligible")
        handoff, tested = support.claims(self.env, subject=self.mod.subject)
        result = prepare.prepare_handoff(declining, e2e_root=self.directory / f"e2e-{QS_KEY}", key=QS_KEY,
                                         output=self.work / "handoff", subject=self.mod.subject, tested=tested,
                                         handoff=handoff, anchor="auto")
        self.assertFalse(result.anchor_eligible)

    def test_validation_requires_the_direct_run_shape(self) -> None:
        def other_branch(manifest: dict[str, Any]) -> None:
            manifest["provenance"]["handoff"].update(branch="release", controller_branch="release")

        def other_tested_commit(manifest: dict[str, Any]) -> None:
            manifest["provenance"]["tested"]["commit"] = "e" * 40

        for change in (other_branch, other_tested_commit):
            with self.subTest(change.__name__):
                shutil.rmtree(self.work / "anchor", ignore_errors=True)
                self.create(QS_KEY)
                with self.assertRaises(MbError) as caught:
                    anchor.validate_anchor_dir(self.edit_anchor(change), key=QS_KEY)
                self.assertEqual(caught.exception.reason, "anchor-source")

    def test_create_cuts_canonical_lossless_reference_frames(self) -> None:
        manifest = self.create(QS_KEY)
        root = self.work / "anchor"
        handoff = json.loads((self.handoffs[QS_KEY] / "manifest.json").read_bytes())
        expectation = json.loads((root / "expectation.json").read_bytes())
        validate_anchor(manifest, expectation=expectation)
        self.assertEqual((root / "expectation.json").read_bytes(),
                         (self.handoffs[QS_KEY] / "expectation.json").read_bytes())
        self.assertEqual(manifest["reference"], {"artifact_nodes": ["fabric-1.20.1", "forge-1.20.1"],
                                                 "minecraft": ["1.20.1"], "loaders": ["fabric", "forge"]})
        self.assertEqual(manifest["source_artifact"], {"id": 31, "name": f"mb-handoff--{QS_KEY}--a2",
                                                       "digest": DIGEST, "run_id": 6100, "run_attempt": 2})
        self.assertEqual(manifest["kit"], handoff["kit"])
        self.assertEqual(manifest["provenance"], {"handoff": handoff["provenance"]["handoff"],
                                                  "tested": handoff["provenance"]["tested"]})
        self.assertEqual(len(manifest["frames"]), len(handoff["frames"]))
        for frame, original in zip(manifest["frames"], handoff["frames"]):
            raw = (self.handoffs[QS_KEY] / original["source"]["path"]).read_bytes()
            stored = (root / frame["source"]["path"]).read_bytes()
            self.assertNotEqual(stored, raw, "the fixture PNG is not canonical, so the anchor re-encodes it")
            self.assertEqual(stored, canonical_png(raw))
            self.assertEqual(frame["source"]["pixel_sha256"], original["source"]["pixel"]["pixel_sha256"])
            self.assertEqual(frame["source"]["pixel"], inspect_png(stored, SizePolicy.exact(192, 108)))
            self.assertEqual({key: value for key, value in frame.items() if key != "source"},
                             {key: value for key, value in original.items() if key != "source"})
        validated = anchor.validate_anchor_dir(root, key=QS_KEY, expected_subject_commit=self.mod.commit,
                                               raw_artifact_id=31, raw_artifact_name=f"mb-handoff--{QS_KEY}--a2",
                                               raw_artifact_digest=DIGEST)
        self.assertEqual(validated, manifest)
        self.assertEqual(validate.validate_bundle(self.invocation, "anchor", root, key=QS_KEY), manifest)

    def test_create_binds_the_uploaded_handoff_identity(self) -> None:
        with self.assertRaisesRegex(MbError, "raw artifact name"):
            self.create(QS_KEY, raw_artifact_name=grammar.handoff_name(QS_KEY, 1))
        with self.assertRaises(MbError):
            self.create(QS_KEY, raw_artifact_digest="sha1:" + "d" * 40)
        with self.assertRaises(MbError):
            self.create(QS_KEY, raw_artifact_id=0)
        self.assertFalse((self.work / "anchor").exists())

    def test_validation_rejects_every_mismatch(self) -> None:
        self.create(QS_KEY)
        root = self.work / "anchor"
        with self.assertRaisesRegex(MbError, "go together"):
            anchor.validate_anchor_dir(root, raw_artifact_id=31)
        for options in ({"raw_artifact_id": 32, "raw_artifact_name": f"mb-handoff--{QS_KEY}--a2",
                         "raw_artifact_digest": DIGEST},
                        {"raw_artifact_id": 31, "raw_artifact_name": f"mb-handoff--{QS_KEY}--a2",
                         "raw_artifact_digest": "sha256:" + "e" * 64},
                        {"key": "mc26.3"}, {"expected_subject_commit": "f" * 40}):
            with self.subTest(options=options), self.assertRaises(MbError):
                anchor.validate_anchor_dir(root, **options)

    def test_validation_requires_canonical_pngs(self) -> None:
        self.create(QS_KEY)
        root = self.work / "anchor"
        manifest = json.loads((root / "manifest.json").read_bytes())
        frame = manifest["frames"][0]
        raw = (self.handoffs[QS_KEY] / json.loads((self.handoffs[QS_KEY] / "manifest.json").read_bytes())
               ["frames"][0]["source"]["path"]).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        old = frame["source"]["path"]
        new = f"images/{digest}.png"
        (root / new).write_bytes(raw)
        pixel = inspect_png(raw, SizePolicy.exact(192, 108))
        for item in manifest["frames"]:
            if item["source"]["path"] == old:
                item["source"].update(path=new, sha256=digest, size=len(raw), pixel=pixel)
        paths = {item["source"]["path"] for item in manifest["frames"]}
        if old not in paths:
            (root / old).unlink()
        manifest["files"] = sorted([record for record in manifest["files"] if record["path"] in paths
                                    or record["path"] == "expectation.json"]
                                   + [{"path": new, "sha256": digest, "size": len(raw)}], key=lambda r: r["path"])
        (root / "manifest.json").write_bytes(canonical_json(manifest))
        validate_anchor(manifest)
        with self.assertRaisesRegex(MbError, "not a canonical"):
            anchor.validate_anchor_dir(root)

    def test_the_composite_commands_through_the_isolated_host(self) -> None:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(KIT / "src"),
                       "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                       "HOME": str(self.work), **self.env, "GITHUB_OUTPUT": str(self.work / "output")}

        def run(*arguments: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, "-P", "-m", "mod_base", *arguments], env=environment,
                                  capture_output=True, text=True, timeout=300, check=False)

        repo = ("--repo", str(self.mod.root))
        completed = run("anchor", "identity", *repo, "--key", QS_KEY, "--handoff", str(self.handoffs[QS_KEY]))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        identity = json.loads(completed.stdout)
        self.assertEqual(identity, {"eligible": True, "name": f"mb-anchor--{QS_KEY}--{self.mod.commit}--6100--a2"})
        self.assertEqual((self.work / "output").read_text(),
                         f"anchor_eligible=true\nanchor_name={identity['name']}\n")
        raw = ("--raw-artifact-id", "31", "--raw-artifact-name", f"mb-handoff--{QS_KEY}--a2",
               "--raw-artifact-digest", DIGEST)
        completed = run("anchor", "create", *repo, "--key", QS_KEY, "--handoff", str(self.handoffs[QS_KEY]), *raw,
                        "--output", str(self.work / "anchor"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed = run("anchor", "validate", "--key", QS_KEY, "--input", str(self.work / "anchor"),
                        "--expected-subject-commit", self.mod.commit, *raw)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed = run("anchor", "validate", "--key", QS_KEY, "--input", str(self.work / "anchor"),
                        "--raw-artifact-id", "31")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.stub.calls, [])

    def test_quick_skin_grace_data(self) -> None:
        config = load_config(self.mod.root)
        self.assertEqual(config.anchor, {"enabled": True, "retention_days": 90, "successor_grace_days": 0})


class BlockPopsAnchorTest(AnchorFlow):
    fixture = "bp_like"

    def test_visual_reference_node_only(self) -> None:
        identity = anchor.anchor_identity(self.invocation, self.handoffs[BP_KEY], key=BP_KEY)
        self.assertEqual(identity.artifact_nodes, ("neoforge-1.21.1",))
        manifest = self.create(BP_KEY)
        self.assertEqual(manifest["reference"], {"artifact_nodes": ["neoforge-1.21.1"], "minecraft": ["1.21.1"],
                                                 "loaders": ["neoforge"]})
        self.assertEqual([lane["lane_id"] for lane in manifest["lanes"]],
                         ["neoforge-1.21.1/ui-regression", "neoforge-1.21.1/in-world"])
        self.assertEqual(len(manifest["frames"]), 3)
        self.assertEqual({lane["jars"]["harness_sha256"] for lane in manifest["lanes"]},
                         {hashlib.sha256(b"harness:neoforge-1.21.1").hexdigest()})
        anchor.validate_anchor_dir(self.work / "anchor", key=BP_KEY)

    def test_an_attested_handoff_never_cuts_an_anchor(self) -> None:
        # Block Pops cuts anchors only from one direct exact-head packaged run, never from an
        # attestation that re-publishes another run's pixels.
        handoff, direct = support.claims(self.env, subject=self.mod.subject)
        tested = {**direct, "run_id": 4000, "commit": "e" * 40, "controller_sha": "e" * 40}
        result = self.prepare(BP_KEY, "attested", tested=tested)
        self.assertEqual(result.manifest["provenance"]["reuse"], "attested")
        self.assertEqual(result.manifest["provenance"]["handoff"]["controller_sha"], self.mod.commit)
        self.assertFalse(result.anchor_eligible)
        self.assertFalse(anchor.anchor_identity(self.invocation, self.work / "attested", key=BP_KEY).eligible)
        with self.assertRaises(MbError) as caught:
            anchor.create_anchor(self.invocation, key=BP_KEY, handoff_dir=self.work / "attested", raw_artifact_id=31,
                                 raw_artifact_name=grammar.handoff_name(BP_KEY, 2), raw_artifact_digest=DIGEST,
                                 output=self.work / "anchor")
        self.assertEqual(caught.exception.reason, "anchor-ineligible")
        self.assertNotIn(("anchor_selection", False), self.stub.calls)

    def test_block_pops_grace_data(self) -> None:
        config = load_config(self.mod.root)
        self.assertEqual(config.anchor, {"enabled": True, "retention_days": 90, "successor_grace_days": 8})


if __name__ == "__main__":
    unittest.main()
