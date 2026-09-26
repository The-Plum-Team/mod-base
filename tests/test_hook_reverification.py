"""Generic core re-verification of hook output (SPEC §4.4): R1 ``collect``, R2 ``expectation``,
R3 ``compose`` (with the baseline owner authentication) and R6 ``authenticate_extensions``, plus
the fail-closed refusal of ``selected`` evidence without a ``compose`` hook."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.evidence import commands
from mod_base.evidence import compact as compact_module
from mod_base.evidence import compose, prepare, validate
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from tests.fixtures.mods import support

KEY = "mc1.20.1"
BP_KEY = hashlib.sha256(b"master").hexdigest()[:24]
SELECTION = {"quick-skin.feature_selection": {"scenarios": ["full"], "baseline_artifact_id": 501}}
#: A selection that re-captures one of the two ``session`` checkpoints: the session lanes mix epochs.
PARTIAL_SELECTION = {"quick-skin.feature_selection": {"captures": ["session.client_b.player_list"],
                                                      "baseline_artifact_id": 501}}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def edit_bundle(root: Path, change: Callable[[dict[str, Any]], None], **files: bytes) -> None:
    """Change ``manifest.json`` and write ``files`` while keeping the inventory consistent."""

    manifest = json.loads((root / "manifest.json").read_bytes())
    for name, data in files.items():
        path = name.replace("__", "/")
        (root / path).write_bytes(data)
        record = {"path": path, "sha256": sha256(data), "size": len(data)}
        manifest["files"] = sorted([item for item in manifest["files"] if item["path"] != path] + [record],
                                   key=lambda item: item["path"])
        for field in ("expectation", "extensions", "selection"):
            if isinstance(manifest.get(field), dict) and manifest[field]["path"] == path:
                manifest[field].update(sha256=record["sha256"], size=record["size"])
    change(manifest)
    (root / "manifest.json").write_bytes(canonical_json(manifest))


class ReverificationFlow(unittest.TestCase):
    """qs-like: a complete handoff (run 4242), its compact as the ``mb-baseline`` of a successful
    Pages run, a ``selected`` handoff (run 4343) re-testing the whole ``full`` scenario and a
    ``selected`` handoff (run 4344) re-capturing one ``session`` checkpoint, both naming that
    baseline."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-rules-test-")).resolve()
        cls.mod = support.materialize("qs_like", cls.directory / "repo")
        cls.fake = FakeGitHub(repository=cls.mod.repository)
        with mock.patch.object(host, "call", support.InProcessHost(api=cls.fake)):
            cls.env = support.environment(cls.mod)
            producer = support.invocation(cls.mod, cls.env, implementation_sha=cls.mod.commit)
            e2e = cls.directory / "e2e"
            support.synthesize(producer, KEY, cls.mod.subject, e2e)
            handoff, tested = support.claims(cls.env, subject=cls.mod.subject)
            cls.complete = prepare.prepare_handoff(producer, e2e_root=e2e, key=KEY, output=cls.directory / "complete",
                                                   subject=cls.mod.subject, tested=tested, handoff=handoff)
            baseline_draft = cls.directory / "baseline-draft.json"
            raw = (cls.directory / "complete/manifest.json").read_bytes()
            baseline_draft.write_bytes(canonical_json(support.selection_draft(
                cls.complete.manifest, raw, artifact_id=11, digest="sha256:" + "a" * 64, size=4096,
                pages_run_id=support.BASELINE_OWNER_RUN_ID)))
            compact_module.compact_bundle(cls.pages(), key=KEY, input_dir=cls.directory / "complete",
                                          selection_path=baseline_draft, output=cls.directory / "baseline")
            cls.baseline_ref = support.seed_baseline(cls.fake, cls.mod, key=KEY, compact_root=cls.directory / "baseline",
                                                     artifact_id=501, tested_run_id=4242)
            cls.selected_env = support.environment(cls.mod, run_id=4343)
            extensions = cls.directory / "extensions.json"
            extensions.write_text(json.dumps(SELECTION))
            handoff, tested = support.claims(cls.selected_env, subject=cls.mod.subject)
            cls.selected = prepare.prepare_handoff(
                support.invocation(cls.mod, cls.selected_env, implementation_sha=cls.mod.commit), e2e_root=e2e,
                key=KEY, output=cls.directory / "selected", subject=cls.mod.subject, tested=tested, handoff=handoff,
                extensions_path=extensions)
            partial_env = support.environment(cls.mod, run_id=4344)
            partial_extensions = cls.directory / "partial-extensions.json"
            partial_extensions.write_text(json.dumps(PARTIAL_SELECTION))
            handoff, tested = support.claims(partial_env, subject=cls.mod.subject)
            # The re-capturing run tested another JAR for as long as it took.
            partial_e2e = cls.directory / "e2e-partial"
            shutil.copytree(e2e, partial_e2e)
            for result in partial_e2e.glob("profiles/*/result.json"):
                record = json.loads(result.read_bytes())
                record.update(jar_sha256=sha256(f"retested:{record['artifact_node']}".encode()),
                              elapsed_s=record["elapsed_s"] + 100)
                result.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            cls.partial = prepare.prepare_handoff(
                support.invocation(cls.mod, partial_env, implementation_sha=cls.mod.commit), e2e_root=partial_e2e,
                key=KEY, output=cls.directory / "partial", subject=cls.mod.subject, tested=tested, handoff=handoff,
                extensions_path=partial_extensions)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    @classmethod
    def pages(cls, mod: support.FixtureMod | None = None):
        return support.invocation(mod or cls.mod, support.pages_environment(mod or cls.mod))

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-rules-case-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.fake = FakeGitHub(repository=self.mod.repository)
        support.seed_baseline(self.fake, self.mod, key=KEY, compact_root=self.directory / "baseline",
                              artifact_id=501, tested_run_id=4242)
        self.stub = support.InProcessHost(api=self.fake)
        self.patch_host(self.stub)

    def patch_host(self, stub: Any) -> None:
        patcher = mock.patch.object(host, "call", stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy(self, source: Path, name: str) -> Path:
        destination = self.work / name
        shutil.copytree(source, destination, symlinks=True)
        return destination

    def draft(self, bundle: Path, **options: Any) -> Path:
        raw = (bundle / "manifest.json").read_bytes()
        values = {"artifact_id": 12, "digest": "sha256:" + "b" * 64, "size": 2048, **options}
        path = self.work / f"draft-{len(list(self.work.glob('draft-*')))}.json"
        path.write_bytes(canonical_json(support.selection_draft(json.loads(raw), raw, **values)))
        return path

    def forked_mod(self, change: Callable[[str], str]) -> support.FixtureMod:
        """A copy of the fixture repository whose working-tree adapter is ``change(source)``."""

        root = self.copy(self.mod.root, "repo")
        adapter = root / "scripts/pages/mod_base_adapter.py"
        adapter.write_text(change(adapter.read_text()), encoding="utf-8")
        return support.FixtureMod(name=self.mod.name, root=root, commit=self.mod.commit, tree=self.mod.tree,
                                  branch=self.mod.branch, repository=self.mod.repository)

    def compose(self, *, mod: support.FixtureMod | None = None, selected: Path | None = None,
                draft: Path | None = None) -> dict[str, Any]:
        selected = selected or self.directory / "selected"
        return compose.compose_selected(self.pages(mod), api=self.fake, key=KEY, selected_dir=selected,
                                        selection_path=draft or self.draft(selected), output=self.work / "composed")


class CollectRuleTest(ReverificationFlow):
    def test_r1_collect_must_reproduce_the_manifest(self) -> None:
        drifted = self.forked_mod(lambda source: source.replace(
            '"runtime_evidence": step["message"]', '"runtime_evidence": step["message"] + " (drift)"'))
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(self.pages(drifted), self.directory / "complete", key=KEY, rederive=False)
        self.assertEqual(caught.exception.reason, "collect-drift")

    def test_r1_a_forged_manifest_is_caught_by_collect(self) -> None:
        handoff = self.copy(self.directory / "complete", "handoff")
        edit_bundle(handoff, lambda manifest: manifest["frames"][0].update(runtime_evidence="PASS forged"))
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(self.pages(), handoff, key=KEY, rederive=False)
        self.assertEqual(caught.exception.reason, "collect-drift")

    def test_r1_collect_is_a_pure_function_of_the_runtime_bytes(self) -> None:
        impure = self.forked_mod(lambda source: source.replace(
            '"elapsed_s": result["elapsed_s"]', '"elapsed_s": float(len(str(runtime_root)))'))
        producer = support.invocation(impure, self.env, implementation_sha=self.mod.commit)
        handoff, tested = support.claims(self.env, subject=self.mod.subject)
        with self.assertRaises(MbError) as caught:
            prepare.prepare_handoff(producer, e2e_root=self.directory / "e2e", key=KEY, output=self.work / "handoff",
                                    subject=self.mod.subject, tested=tested, handoff=handoff)
        self.assertEqual(caught.exception.reason, "collect-drift")
        self.assertFalse((self.work / "handoff").exists())

    def test_r1_every_source_path_lies_inside_runtime_files(self) -> None:
        leaky = self.forked_mod(lambda source: source.replace(
            '        files.append(source)\n        by_frame', '        by_frame'))
        producer = support.invocation(leaky, self.env, implementation_sha=self.mod.commit)
        handoff, tested = support.claims(self.env, subject=self.mod.subject)
        with self.assertRaisesRegex(MbError, "listed runtime .png"):
            prepare.prepare_handoff(producer, e2e_root=self.directory / "e2e", key=KEY, output=self.work / "handoff",
                                    subject=self.mod.subject, tested=tested, handoff=handoff)

    def test_r1_cross_check_needs_the_mods_reported_metrics(self) -> None:
        silent = self.forked_mod(lambda source: source.replace(
            ',\n                       "reported_pixel": report["pixel_validation"]["screenshots"][capture["step"]]', ''))
        producer = support.invocation(silent, self.env, implementation_sha=self.mod.commit)
        handoff, tested = support.claims(self.env, subject=self.mod.subject)
        with self.assertRaises(MbError) as caught:
            prepare.prepare_handoff(producer, e2e_root=self.directory / "e2e", key=KEY, output=self.work / "handoff",
                                    subject=self.mod.subject, tested=tested, handoff=handoff)
        self.assertEqual(caught.exception.reason, "metrics-cross-check")


class ExpectationRuleTest(ReverificationFlow):
    def test_r2_adapter_drift_is_caught_at_validation_and_compaction(self) -> None:
        drifted = self.forked_mod(lambda source: source.replace('"label": target["label"]',
                                                                '"label": target["label"] + " drift"'))
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(support.invocation(drifted, self.env), self.directory / "complete", key=KEY)
        self.assertEqual(caught.exception.reason, "expectation-drift")
        with self.assertRaises(MbError) as caught:
            compact_module.compact_bundle(self.pages(drifted), key=KEY, input_dir=self.directory / "complete",
                                          selection_path=self.draft(self.directory / "complete"),
                                          output=self.work / "compact")
        self.assertEqual(caught.exception.reason, "expectation-drift")

    def test_r2_a_forged_embedded_expectation_is_caught(self) -> None:
        handoff = self.copy(self.directory / "complete", "handoff")
        expectation = json.loads((handoff / "expectation.json").read_bytes())
        expectation["label"] = "Minecraft 1.20.1 forged"
        edit_bundle(handoff, lambda manifest: None, **{"expectation.json": canonical_json(expectation)})
        validate.validate_handoff_dir(self.pages(), handoff, key=KEY, rederive=False)  # structurally valid
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(support.invocation(self.mod, self.env), handoff, key=KEY)
        self.assertEqual(caught.exception.reason, "expectation-drift")
        with self.assertRaises(MbError) as caught:
            compact_module.compact_bundle(self.pages(), key=KEY, input_dir=handoff, selection_path=self.draft(handoff),
                                          output=self.work / "compact")
        self.assertEqual(caught.exception.reason, "expectation-drift")


class BlockPopsExpectationRuleTest(unittest.TestCase):
    def test_r2_binds_the_handoff_runs_event_projection(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="mb-rules-bp-")).resolve()
        self.addCleanup(shutil.rmtree, directory, True)
        mod = support.materialize("bp_like", directory / "repo")
        environ = support.environment(mod, event="schedule")
        with mock.patch.object(host, "call", support.InProcessHost()):
            producer = support.invocation(mod, environ, implementation_sha=mod.commit)
            support.synthesize(producer, BP_KEY, mod.subject, directory / "e2e", event="schedule")
            handoff, tested = support.claims(environ, subject=mod.subject)
            result = prepare.prepare_handoff(producer, e2e_root=directory / "e2e", key=BP_KEY,
                                             output=directory / "handoff", subject=mod.subject, tested=tested,
                                             handoff=handoff)
            self.assertEqual(result.manifest["lanes"][0]["profile"], "scheduled-anchors")
            validate.validate_handoff_dir(support.invocation(mod, environ), directory / "handoff", key=BP_KEY)
            dispatched = {**environ, "GITHUB_EVENT_NAME": "workflow_dispatch"}
            with self.assertRaises(MbError) as caught:
                validate.validate_handoff_dir(support.invocation(mod, dispatched), directory / "handoff", key=BP_KEY)
            self.assertEqual(caught.exception.reason, "expectation-drift")
            raw = (directory / "handoff/manifest.json").read_bytes()
            pages = support.invocation(mod, support.pages_environment(mod))
            for event, expected in (("workflow_dispatch", "expectation-drift"), ("schedule", None)):
                draft = directory / f"draft-{event}.json"
                draft.write_bytes(canonical_json(support.selection_draft(
                    result.manifest, raw, artifact_id=12, digest="sha256:" + "b" * 64, size=10, event=event)))
                output = directory / f"compact-{event}"
                if expected is None:
                    compact_module.compact_bundle(pages, key=BP_KEY, input_dir=directory / "handoff",
                                                  selection_path=draft, output=output)
                    continue
                with self.assertRaises(MbError) as caught:
                    compact_module.compact_bundle(pages, key=BP_KEY, input_dir=directory / "handoff",
                                                  selection_path=draft, output=output)
                self.assertEqual(caught.exception.reason, expected)


    def test_r2_the_tested_runs_own_projection_must_agree(self) -> None:
        # An attested handoff (a workflow_dispatch run re-publishing packaged run 4000) projects
        # pr-anchors; its authenticated tested run may not have been a scheduled run, whose own
        # projection is scheduled-anchors (Block Pops authenticate_source).
        directory = Path(tempfile.mkdtemp(prefix="mb-rules-bp-")).resolve()
        self.addCleanup(shutil.rmtree, directory, True)
        mod = support.materialize("bp_like", directory / "repo")
        environ = support.environment(mod, run_id=4500)
        title = f"Packaged E2E / {mod.commit}"
        with mock.patch.object(host, "call", support.InProcessHost()):
            producer = support.invocation(mod, environ, implementation_sha=mod.commit)
            support.synthesize(producer, BP_KEY, mod.subject, directory / "e2e")
            handoff, tested = support.claims(environ, subject=mod.subject)
            tested = {**tested, "run_id": 4000}
            result = prepare.prepare_handoff(producer, e2e_root=directory / "e2e", key=BP_KEY,
                                             output=directory / "handoff", subject=mod.subject, tested=tested,
                                             handoff=handoff)
            self.assertEqual(result.manifest["provenance"]["reuse"], "attested")
            self.assertEqual(result.manifest["lanes"][0]["profile"], "pr-anchors")
            raw = (directory / "handoff/manifest.json").read_bytes()
            pages = support.invocation(mod, support.pages_environment(mod))
            for tested_event, expected in (("schedule", "expectation-drift"), ("pull_request", None)):
                draft = directory / f"draft-{tested_event}.json"
                draft.write_bytes(canonical_json(support.selection_draft(
                    result.manifest, raw, artifact_id=12, digest="sha256:" + "b" * 64, size=10,
                    tested_run=support.run_record(tested, event=tested_event, display_title=title),
                    attestation_job={"name": "Attest", "id": 99, "conclusion": "success"}, display_title=title)))
                output = directory / f"compact-{tested_event}"
                if expected is None:
                    compact_module.compact_bundle(pages, key=BP_KEY, input_dir=directory / "handoff",
                                                  selection_path=draft, output=output)
                    continue
                with self.assertRaises(MbError) as caught:
                    compact_module.compact_bundle(pages, key=BP_KEY, input_dir=directory / "handoff",
                                                  selection_path=draft, output=output)
                self.assertEqual(caught.exception.reason, expected)
                self.assertIn("'schedule' projection", str(caught.exception))
                self.assertFalse(output.exists())


class ComposeRuleTest(ReverificationFlow):
    def test_r3_composes_selected_evidence_with_its_authenticated_baseline(self) -> None:
        manifest = self.compose()
        root = self.work / "composed"
        selection = json.loads((root / "selection.json").read_bytes())
        self.assertEqual(manifest["scope"]["kind"], "composed")
        self.assertEqual(selection["composition"]["baseline_artifact"],
                         {**self.baseline_ref, "owner_run_id": support.BASELINE_OWNER_RUN_ID})
        self.assertEqual(manifest["scope"]["components"]["baseline"]["artifact_id"], 501)
        self.assertEqual(selection["binding"]["mode"], "reencode-identical")
        self.assertEqual(selection["expectation_sha256"], manifest["expectation"]["sha256"])
        self.assertEqual(selection["extensions_verified"], [])
        self.assertIsNone(manifest["extensions"])
        epochs = {frame["frame_id"]: frame["epoch"] for frame in manifest["frames"]}
        self.assertEqual({frame_id for frame_id, epoch in epochs.items() if epoch == "selected"},
                         {frame["frame_id"] for frame in self.selected.manifest["frames"]})
        self.assertEqual(len(manifest["frames"]), len(self.complete.manifest["frames"]))
        for frame in manifest["frames"]:
            run_id = 4343 if frame["epoch"] == "selected" else 4242
            self.assertEqual(frame["tested"]["run_id"], run_id)
        self.assertIn(("compose", True), self.stub.calls)
        validate.validate_compact_dir(self.pages(), root, key=KEY, bind_raw=self.directory / "selected")
        self.assertEqual(manifest, json.loads((root / "manifest.json").read_bytes()))

    def test_selected_evidence_without_a_compose_hook_is_rejected(self) -> None:
        without = self.forked_mod(lambda source: source.replace("def compose(", "def _compose("))
        with self.assertRaises(MbError) as caught:
            self.compose(mod=without)
        self.assertEqual(caught.exception.reason, "compose-unsupported")
        self.assertFalse((self.work / "composed").exists())

    def test_the_compose_command_caps_its_api_reads(self) -> None:
        captured: dict[str, Any] = {}

        def fake_compose(invocation: Any, *, api: Any, **arguments: Any) -> dict[str, Any]:
            captured["api"] = api
            return {}

        args = argparse.Namespace(repo=self.mod.root, config=None, key=KEY, selected=self.directory / "selected",
                                  selection=self.work / "draft.json", output=self.work / "composed")
        with mock.patch.dict(os.environ, support.pages_environment(self.mod)), \
                mock.patch.object(compose, "compose_selected", fake_compose):
            self.assertEqual(commands.run_compose(args), 0)
        self.assertEqual(captured["api"]._max_requests, lim.MAX_PAGES_API_READS)

    def test_compose_takes_only_a_selected_handoff(self) -> None:
        with self.assertRaisesRegex(MbError, "selected handoff"):
            self.compose(selected=self.directory / "complete")

    def test_r3_rejects_a_tampered_composition(self) -> None:
        def selected_tested_everywhere(output: Path) -> None:
            def change(manifest: dict[str, Any]) -> None:
                for frame in manifest["frames"]:
                    frame["tested"]["run_id"] = 4343
            edit_bundle(output, change)

        def mislabelled_epoch(output: Path) -> None:
            def change(manifest: dict[str, Any]) -> None:
                for frame in manifest["frames"]:
                    frame["epoch"] = "selected"
            edit_bundle(output, change)

        def foreign_selected_identity(output: Path) -> None:
            edit_bundle(output, lambda manifest: manifest["scope"]["components"].update(
                selected_manifest_sha256="0" * 64))

        def foreign_baseline_manifest(output: Path) -> None:
            edit_bundle(output, lambda manifest: manifest["scope"]["components"]["baseline"].update(
                manifest_sha256="0" * 64))

        def dropped_frame(output: Path) -> None:
            edit_bundle(output, lambda manifest: manifest["frames"].pop())

        def swapped_baseline_image(output: Path) -> None:
            def change(manifest: dict[str, Any]) -> None:
                baseline = [frame for frame in manifest["frames"] if frame["epoch"] == "baseline"]
                first = baseline[0]
                second = next(frame for frame in baseline if frame["derivative"] != first["derivative"])
                first["derivative"], second["derivative"] = second["derivative"], first["derivative"]
            edit_bundle(output, change)

        def stale_baseline_for_selected(output: Path) -> None:
            # Publish the older baseline for exactly what the selection re-tested: every selected
            # frame relabelled as a faithful baseline frame (same bytes, the baseline's tested run).
            def change(manifest: dict[str, Any]) -> None:
                lanes = {lane["lane_id"]: lane for lane in manifest["lanes"]}
                stale = {name: value for name, value in next(
                    frame["tested"] for frame in manifest["frames"] if frame["epoch"] == "baseline").items()
                    if name != "jar_sha256"}
                for frame in manifest["frames"]:
                    if frame["epoch"] == "selected":
                        frame["epoch"] = "baseline"
                        frame["tested"] = {**stale, "jar_sha256": lanes[frame["lane_id"]]["jars"]["production_sha256"]}
            edit_bundle(output, change)

        for tamper in (selected_tested_everywhere, mislabelled_epoch, foreign_selected_identity,
                       foreign_baseline_manifest, dropped_frame, swapped_baseline_image, stale_baseline_for_selected):
            with self.subTest(tamper.__name__):
                shutil.rmtree(self.work / "composed", ignore_errors=True)
                self.patch_host(TamperingHost(api=self.fake, tamper=tamper))
                with self.assertRaises(MbError):
                    self.compose()
                self.assertFalse((self.work / "composed").exists())

    def test_r6_changed_extensions_are_verified_again(self) -> None:
        reference = {"repository": self.mod.repository, "run_id": 4343, "tested_sha": self.mod.commit}
        added = canonical_json({"quick-skin.runtime_source": reference})

        def add_extension(output: Path) -> None:
            record = {"path": "extensions.json", "sha256": sha256(added), "size": len(added),
                      "names": ["quick-skin.runtime_source"]}
            edit_bundle(output, lambda manifest: manifest.update(extensions=record), **{"extensions.json": added})

        stub = TamperingHost(api=self.fake, tamper=add_extension)
        self.patch_host(stub)
        with self.assertRaises(Exception):
            self.compose()  # the fixture adapter finds no successful tested run 4343 through the API
        self.assertIn(("authenticate_extensions", True), stub.calls)
        shutil.rmtree(self.work / "composed", ignore_errors=True)
        self.fake.add_run({"id": 4343, "run_attempt": 1, "path": support.SOURCE_WORKFLOW, "event": "workflow_dispatch",
                           "status": "completed", "conclusion": "success", "head_branch": "master",
                           "head_sha": self.mod.commit, "head_repository": {"full_name": self.mod.repository},
                           "created_at": "2026-09-01T12:00:00Z"})
        manifest = self.compose()
        selection = json.loads((self.work / "composed/selection.json").read_bytes())
        self.assertEqual(manifest["extensions"]["names"], ["quick-skin.runtime_source"])
        self.assertEqual(selection["extensions_verified"], ["quick-skin.runtime_source"])

    def test_r6_rules(self) -> None:
        manifest = {"extensions": {"names": ["a.b", "c.d"]}, "provenance": {"reuse": "delegated"}}
        with self.assertRaisesRegex(MbError, "were not verified"):
            validate.check_extensions_verified(manifest, {"verified": ["a.b"], "reuse_verified": True})
        with self.assertRaisesRegex(MbError, "delegated reuse"):
            validate.check_extensions_verified(manifest, {"verified": ["a.b", "c.d"], "reuse_verified": False})
        self.assertEqual(validate.check_extensions_verified(
            manifest, {"verified": ["a.b", "c.d"], "reuse_verified": True}), ["a.b", "c.d"])
        self.assertEqual(validate.check_extensions_verified(
            {"extensions": None, "provenance": {"reuse": "none"}}, {"verified": [], "reuse_verified": False}), [])


class PartialCompositionTest(ReverificationFlow):
    """A selection that re-captures only some frames of a lane (Quick Skin's hud-preview: 2 of 63
    ``full`` captures) composes per frame, as Quick Skin's schema-7 view did (defect of v0.9.0/1)."""

    def compose_partial(self) -> dict[str, Any]:
        return self.compose(selected=self.directory / "partial")

    def test_a_partially_recaptured_lane_passes_r3(self) -> None:
        manifest = self.compose_partial()
        baseline = json.loads((self.directory / "baseline/manifest.json").read_bytes())
        baseline_lanes = {lane["lane_id"]: lane for lane in baseline["lanes"]}
        partial_lanes = {lane["lane_id"]: lane for lane in self.partial.manifest["lanes"]}
        by_lane: dict[str, set[str]] = {}
        for frame in manifest["frames"]:
            by_lane.setdefault(frame["lane_id"], set()).add(frame["epoch"])
            source = partial_lanes if frame["epoch"] == "selected" else baseline_lanes
            self.assertEqual(frame["tested"]["run_id"], 4344 if frame["epoch"] == "selected" else 4242)
            self.assertEqual(frame["tested"]["jar_sha256"], source[frame["lane_id"]]["jars"]["production_sha256"])
        self.assertEqual({frame["frame_id"] for frame in manifest["frames"] if frame["epoch"] == "selected"},
                         {frame["frame_id"] for frame in self.partial.manifest["frames"]})
        self.assertEqual(4, sum(frame["epoch"] == "selected" for frame in manifest["frames"]) * 2)
        for lane in manifest["lanes"]:
            lane_id = lane["lane_id"]
            if lane_id.endswith("/session"):
                self.assertEqual(by_lane[lane_id], {"baseline", "selected"})
                self.assertEqual(partial_lanes[lane_id]["roles"], ["client_a", "client_b"])
                self.assertEqual({name: value for name, value in lane.items() if name != "baseline_run"},
                                 partial_lanes[lane_id], "the lane record is the selected source lane")
                self.assertEqual(lane["jars"], partial_lanes[lane_id]["jars"])
                self.assertNotEqual(lane["jars"], baseline_lanes[lane_id]["jars"])
                self.assertEqual(lane["baseline_run"], {name: baseline_lanes[lane_id][name]
                                                        for name in ("profile", "status", "elapsed_s", "jars")})
            else:
                self.assertEqual(by_lane[lane_id], {"baseline"})
                self.assertEqual(lane, baseline_lanes[lane_id])
        self.assertEqual(manifest["comparisons"], baseline["comparisons"])
        validate.validate_compact_dir(self.pages(), self.work / "composed", key=KEY,
                                      bind_raw=self.directory / "partial")

    def test_r3_rejects_a_partial_lane_that_hides_an_epoch(self) -> None:
        def session_lanes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
            return [lane for lane in manifest["lanes"] if lane["lane_id"].endswith("/session")]

        def dropped_baseline_run(output: Path) -> None:
            edit_bundle(output, lambda manifest: [lane.pop("baseline_run") for lane in session_lanes(manifest)])

        def selected_run_as_baseline_run(output: Path) -> None:
            def change(manifest: dict[str, Any]) -> None:
                for lane in session_lanes(manifest):
                    lane["baseline_run"]["jars"] = dict(lane["jars"])
                for frame in manifest["frames"]:
                    if frame["lane_id"].endswith("/session") and frame["epoch"] == "baseline":
                        frame["tested"]["jar_sha256"] = next(lane["jars"]["production_sha256"]
                                                             for lane in session_lanes(manifest)
                                                             if lane["lane_id"] == frame["lane_id"])
            edit_bundle(output, change)

        def baseline_record_for_retested_lane(output: Path) -> None:
            def change(manifest: dict[str, Any]) -> None:
                for lane in session_lanes(manifest):
                    run = lane.pop("baseline_run")
                    lane.update(run)
                for frame in manifest["frames"]:
                    if frame["lane_id"].endswith("/session"):
                        frame["tested"]["jar_sha256"] = next(lane["jars"]["production_sha256"]
                                                             for lane in session_lanes(manifest)
                                                             if lane["lane_id"] == frame["lane_id"])
                        frame["epoch"] = "baseline"
            edit_bundle(output, change)

        def selected_roles_only(output: Path) -> None:
            edit_bundle(output, lambda manifest: [lane.update(roles=["client_b"]) for lane in session_lanes(manifest)])

        for tamper in (dropped_baseline_run, selected_run_as_baseline_run, baseline_record_for_retested_lane,
                       selected_roles_only):
            with self.subTest(tamper.__name__):
                shutil.rmtree(self.work / "composed", ignore_errors=True)
                self.patch_host(TamperingHost(api=self.fake, tamper=tamper))
                with self.assertRaises(MbError):
                    self.compose_partial()
                self.assertFalse((self.work / "composed").exists())

    def test_r3_refuses_a_retested_lane_whose_selection_ran_only_some_roles(self) -> None:
        """Every published lane record is one an authenticated source holds: a selected run of only
        some of a lane's roles cannot be published under the complete lane's roles."""

        baseline = json.loads((self.directory / "baseline/manifest.json").read_bytes())
        baseline_lanes = {lane["lane_id"]: lane for lane in baseline["lanes"]}
        partial_lanes = {lane["lane_id"]: lane for lane in self.partial.manifest["lanes"]}
        lane_id = next(lane_id for lane_id in partial_lanes if lane_id.endswith("/session"))
        run_fields = ("profile", "status", "elapsed_s", "jars")
        composed = {**partial_lanes[lane_id],
                    "baseline_run": {name: baseline_lanes[lane_id][name] for name in run_fields}}
        compose._check_lane(composed, partial_lanes, baseline_lanes, {"baseline", "selected"})
        one_role = {**partial_lanes[lane_id], "roles": ["client_b"]}
        with self.assertRaisesRegex(MbError, "must run every role"):
            compose._check_lane(composed, {**partial_lanes, lane_id: one_role}, baseline_lanes,
                                {"baseline", "selected"})
        with self.assertRaisesRegex(MbError, "must run every role"):
            compose._check_lane({**composed, "roles": ["client_b"]}, partial_lanes, baseline_lanes,
                                {"baseline", "selected"})

    def test_the_selection_captures_both_partners_of_a_comparison(self) -> None:
        one_partner = {"quick-skin.feature_selection": {"captures": ["full.client_a.skin_apply"],
                                                        "baseline_artifact_id": 501}}
        with self.assertRaises(MbError) as caught:
            support.run_in_process(self.pages(), "expectation", {
                "target": {"key": KEY, "label": "Minecraft 1.20.1", "subject": self.mod.subject,
                           "matrix_sha256": self.partial.manifest["matrix_sha256"],
                           "contract_sha256": self.partial.manifest["contract_sha256"]},
                "tested_run": None, "extensions": one_partner})
        self.assertIn("one partner of comparison", str(caught.exception))


class TamperingHost(support.InProcessHost):
    """The in-process host, but the ``compose`` hook's output is altered before the core sees it."""

    def __init__(self, *, api: Any, tamper: Callable[[Path], None]) -> None:
        super().__init__(api=api)
        self.tamper = tamper

    def __call__(self, invocation_: Any, hook: str, arguments: Any, *, network: bool = False) -> Any:
        result = super().__call__(invocation_, hook, arguments, network=network)
        if hook == "compose":
            self.tamper(Path(arguments["output_dir"]))
        return result


class BaselineOwnerTest(ReverificationFlow):
    def authenticate(self, fake: FakeGitHub, baseline: dict[str, Any] | None = None, key: str = KEY):
        return compose.authenticate_baseline(self.pages(), fake, key=key, baseline=baseline or self.baseline_ref)

    def reseeded(self, **options: Any) -> tuple[FakeGitHub, dict[str, Any]]:
        fake = FakeGitHub(repository=self.mod.repository)
        reference = support.seed_baseline(fake, self.mod, key=KEY, compact_root=self.directory / "baseline",
                                          artifact_id=501, tested_run_id=4242, **options)
        return fake, reference

    def test_the_authenticated_owner(self) -> None:
        artifact = self.authenticate(self.fake)
        self.assertEqual((artifact.id, artifact.run_id), (501, support.BASELINE_OWNER_RUN_ID))

    def test_owner_failures(self) -> None:
        cases = {
            "not a pages run": {"workflow_path": support.SOURCE_WORKFLOW},
            "refresh job failed": {"job_conclusion": "failure"},
            "uploaded after the retention step": {"created_at": "2026-09-01T13:06:01Z"},
            "uploaded before the retention step": {"created_at": "2026-09-01T13:03:59Z"},
        }
        for label, options in cases.items():
            with self.subTest(label):
                fake, reference = self.reseeded(**options)
                with self.assertRaises(MbError) as caught:
                    self.authenticate(fake, reference)
                self.assertIn(caught.exception.reason, {"baseline-owner", "run-provenance", "job-graph"})

    def test_the_named_artifact_must_be_exactly_the_baseline(self) -> None:
        cases = [
            {**self.baseline_ref, "digest": "sha256:" + "0" * 64},
            {**self.baseline_ref, "name": grammar.baseline_name("mc26.3", self.mod.commit, 4242)},
            {**self.baseline_ref, "name": grammar.cache_name(KEY, self.mod.commit)},
        ]
        for baseline in cases:
            with self.subTest(baseline=baseline["name"]), self.assertRaises(MbError):
                self.authenticate(self.fake, baseline)
        fake, reference = self.reseeded()
        fake.add_artifact({"id": 501, "name": reference["name"], "created_at": "2026-09-01T13:05:00Z",
                           "expired": True, "workflow_run": {"id": support.BASELINE_OWNER_RUN_ID,
                                                             "head_branch": "master", "head_sha": self.mod.commit}},
                          support.zip_directory(self.directory / "baseline"))
        with self.assertRaisesRegex(MbError, "expired"):
            self.authenticate(fake, reference)


if __name__ == "__main__":
    unittest.main()
