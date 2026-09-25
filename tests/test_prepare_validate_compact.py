"""Producer -> handoff -> compact (SPEC §3.2, §3.3, §4.7): prepare, validate, compact, cache
re-emission and the ``--bind-raw`` byte identity, on the qs-like and bp-like fixture mods."""

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
from mod_base.errors import MbError, Unavailable
from mod_base.evidence import compact as compact_module
from mod_base.evidence import expectation as expectation_module
from mod_base.evidence import prepare, validate
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_png
from mod_base.imaging.png import pattern_png
from mod_base.imaging.webp import derive_webp
from mod_base.io.atomic_directory import AtomicDirectoryError
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import (
    check_compact_selection,
    thumbnail_size,
    validate_compact,
    validate_handoff,
    validate_selection,
)
from mod_base.runtime import build_invocation
from tests.fixtures.mods import support

KEY = "mc1.20.1"
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The production isolated host (the flows patch ``host.call`` with the in-process one).
REAL_CALL = host.call


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Flow(unittest.TestCase):
    """A qs-like repository with one synthesized E2E output and one prepared handoff (shared,
    read-only); tests copy what they tamper with."""

    fixture = "qs_like"
    key = KEY
    event = "workflow_dispatch"

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-flow-test-")).resolve()
        cls.mod = support.materialize(cls.fixture, cls.directory / "repo")
        if cls.fixture == "bp_like":
            cls.key = hashlib.sha256(b"master").hexdigest()[:24]
        cls.env = support.environment(cls.mod, event=cls.event)
        cls.e2e = cls.directory / "e2e"
        with mock.patch.object(host, "call", support.InProcessHost()):
            cls.expected = support.synthesize(cls.producer(), cls.key, cls.mod.subject, cls.e2e, event=cls.event)
            cls.handoff_claim, cls.tested_claim = support.claims(cls.env, subject=cls.mod.subject)
            cls.prepared = cls.run_prepare(cls.e2e, cls.directory / "handoff")
        cls.handoff = cls.directory / "handoff"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    @classmethod
    def producer(cls, environ: dict[str, str] | None = None):
        return support.invocation(cls.mod, environ or cls.env, implementation_sha=cls.mod.commit)

    @classmethod
    def run_prepare(cls, e2e: Path, output: Path, **options: Any) -> prepare.PrepareResult:
        arguments = {"e2e_root": e2e, "key": cls.key, "output": output, "subject": cls.mod.subject,
                     "tested": cls.tested_claim, "handoff": cls.handoff_claim, **options}
        return prepare.prepare_handoff(cls.producer(), **arguments)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-flow-case-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.host = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy(self, source: Path, name: str) -> Path:
        destination = self.work / name
        shutil.copytree(source, destination, symlinks=True)
        return destination

    def pages(self, **options: Any):
        return support.invocation(self.mod, support.pages_environment(self.mod, **options))

    def draft_for(self, bundle: Path, **options: Any) -> Path:
        raw = (bundle / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        values = {"artifact_id": 11, "digest": "sha256:" + "a" * 64, "size": 4096, "event": self.event}
        values.update(options)
        draft = support.selection_draft(manifest, raw, **values)
        path = self.work / f"draft-{len(list(self.work.glob('draft-*')))}.json"
        path.write_bytes(canonical_json(draft))
        return path

    def compact(self, source: Path, output_name: str = "compact", **draft: Any) -> dict[str, Any]:
        return compact_module.compact_bundle(self.pages(), key=self.key, input_dir=source,
                                             selection_path=self.draft_for(source, **draft),
                                             output=self.work / output_name)

    def forked(self, change) -> support.FixtureMod:
        """A copy of the fixture repository whose working-tree adapter is ``change(source)``."""

        root = self.copy(self.mod.root, "forked-repo")
        adapter = root / "scripts/pages/mod_base_adapter.py"
        source = adapter.read_text(encoding="utf-8")
        changed = change(source)
        self.assertNotEqual(changed, source, "the fork must change the adapter")
        adapter.write_text(changed, encoding="utf-8")
        return support.FixtureMod(name=self.mod.name, root=root, commit=self.mod.commit, tree=self.mod.tree,
                                  branch=self.mod.branch, repository=self.mod.repository)

    def with_source_config(self, **source: Any) -> Path:
        """This fixture's config with ``source`` fields replaced, as a new file."""

        data = json.loads((self.mod.root / "site/mod-base.json").read_text())
        data["source"].update(source)
        path = self.work / f"config-{len(list(self.work.glob('config-*')))}.json"
        path.write_text(json.dumps(data))
        return path

    def rewrite_result(self, e2e: Path, change) -> None:
        """Apply ``change`` to every packaged result.json of ``e2e``."""

        for path in sorted(e2e.glob("profiles/*/result.json")):
            result = json.loads(path.read_text())
            change(path, result)
            path.write_text(json.dumps(result))


class PrepareTest(Flow):
    def test_prepared_handoff_is_the_exact_documented_bundle(self) -> None:
        manifest = self.prepared.manifest
        self.assertEqual(json.loads((self.handoff / "manifest.json").read_bytes()), manifest)
        expectation = json.loads((self.handoff / "expectation.json").read_bytes())
        validate_handoff(manifest, expectation=expectation, allowed_extensions={"quick-skin.runtime_source"})
        self.assertEqual(expectation, self.expected)
        self.assertEqual(manifest["kit"]["sha"], support.KIT_SHA)
        self.assertEqual(manifest["repository"], self.mod.repository)
        self.assertEqual(manifest["provenance"]["reuse"], "none")
        self.assertEqual(manifest["provenance"]["coverage_sha"], self.mod.commit)
        self.assertEqual(manifest["provenance"]["handoff"]["run_id"], 4242)
        self.assertIsNone(manifest["extensions"])
        self.assertEqual(manifest["scope"], {"kind": "complete"})
        self.assertEqual(len(manifest["lanes"]), 4)
        self.assertEqual(len(manifest["frames"]), 8)
        self.assertEqual([lane["elapsed_s"] for lane in manifest["lanes"]], [2.5, 3.5, 4.5, 5.5])
        self.assertEqual({lane["profile"] for lane in manifest["lanes"]}, {"pr"})
        paths = {record["path"] for record in manifest["files"]}
        self.assertEqual({path for path in paths if not path.startswith("runtime/")}, {"expectation.json"})
        self.assertEqual(len([path for path in paths if path.endswith("/result.json")]), 4)
        self.assertTrue(all(frame["runtime_evidence"].startswith("PASS ") for frame in manifest["frames"]))
        for frame in manifest["frames"]:
            data = (self.handoff / frame["source"]["path"]).read_bytes()
            self.assertEqual(frame["source"]["pixel"], inspect_png(data, SizePolicy.exact(192, 108)))
        self.assertTrue(self.prepared.anchor_eligible)

    def test_only_declared_runtime_files_are_copied(self) -> None:
        e2e = self.copy(self.e2e, "e2e")
        (e2e / "server.log").write_text("noise")
        (e2e / "profiles/extra").mkdir()
        (e2e / "profiles/extra/notes.txt").write_text("noise")
        result = self.run_prepare(e2e, self.work / "handoff")
        self.assertEqual([record["path"] for record in result.manifest["files"]],
                         [record["path"] for record in self.prepared.manifest["files"]])

    def test_the_adapter_target_must_cover_the_exact_subject(self) -> None:
        for subject in ({**self.mod.subject, "tree": "0" * 40}, {**self.mod.subject, "commit": "0" * 40},
                        {**self.mod.subject, "branch": "main"}):
            with self.subTest(subject=subject), self.assertRaises(Unavailable):
                expectation_module.target_for_key(self.producer(), KEY, subject=subject)
        with self.assertRaises(Unavailable):
            expectation_module.target_for_key(self.producer(), "mc9.9", subject=self.mod.subject)

    def test_expect_derives_the_expectation_of_the_checked_out_head(self) -> None:
        document = expectation_module.run_expect(support.invocation(self.mod, self.env), key=KEY, tested_run_json=None,
                                                 extensions=None, output=self.work / "expectation.json")
        self.assertEqual(document, self.expected)
        self.assertEqual((self.work / "expectation.json").read_bytes(), canonical_json(self.expected))
        with self.assertRaises(OSError):
            expectation_module.run_expect(support.invocation(self.mod, self.env), key=KEY, tested_run_json=None,
                                          extensions=None, output=self.work / "expectation.json")

    def test_output_must_be_new(self) -> None:
        (self.work / "handoff").mkdir()
        with self.assertRaises(AtomicDirectoryError):
            self.run_prepare(self.e2e, self.work / "handoff")

    def test_claims_must_name_the_source_workflow(self) -> None:
        foreign = {**self.handoff_claim, "workflow_path": ".github/workflows/other.yml"}
        with self.assertRaisesRegex(MbError, "not a .github/workflows/on-demand-e2e.yml run"):
            self.run_prepare(self.e2e, self.work / "handoff", handoff=foreign)
        self.assertFalse((self.work / "handoff").exists())

    def test_a_distinct_tested_run_needs_a_configured_reuse(self) -> None:
        tested = {**self.tested_claim, "run_id": 4000}
        with self.assertRaisesRegex(MbError, "neither delegated nor attested"):
            self.run_prepare(self.e2e, self.work / "handoff", tested=tested)

    def test_delegated_reuse_through_the_declared_extension(self) -> None:
        tested = {**self.tested_claim, "run_id": 4000, "controller_sha": self.mod.commit}
        extensions = self.work / "extensions.json"
        reference = {"repository": self.mod.repository, "run_id": 4000, "tested_sha": self.mod.commit}
        extensions.write_text(json.dumps({"quick-skin.runtime_source": reference}))
        result = self.run_prepare(self.e2e, self.work / "handoff", tested=tested, extensions_path=extensions)
        self.assertEqual(result.manifest["provenance"]["reuse"], "delegated")
        self.assertEqual(result.manifest["extensions"]["names"], ["quick-skin.runtime_source"])
        written = (self.work / "handoff/extensions.json").read_bytes()
        self.assertEqual(written, canonical_json({"quick-skin.runtime_source": reference}))
        extensions.write_text(json.dumps({"quick-skin.undeclared": {}}))
        with self.assertRaisesRegex(MbError, "undeclared"):
            self.run_prepare(self.e2e, self.work / "other", tested=tested, extensions_path=extensions)

    def test_cross_check_mismatch_fails(self) -> None:
        e2e = self.copy(self.e2e, "e2e")

        def drift(path: Path, result: dict) -> None:
            report = result["reports"]["client_a"]
            step = next(iter(report["pixel_validation"]["screenshots"]))
            report["pixel_validation"]["screenshots"][step]["luma_entropy"] += 0.001

        self.rewrite_result(e2e, drift)
        with self.assertRaises(MbError) as caught:
            self.run_prepare(e2e, self.work / "handoff")
        self.assertEqual(caught.exception.reason, "metrics-cross-check")
        self.assertFalse((self.work / "handoff").exists())

    def test_comparison_cross_check_mismatch_fails(self) -> None:
        e2e = self.copy(self.e2e, "e2e")

        def drift(path: Path, result: dict) -> None:
            for report in result["reports"].values():
                for metrics in report["pixel_validation"]["comparisons"].values():
                    metrics["rms_difference"] += 1.0

        self.rewrite_result(e2e, drift)
        with self.assertRaises(MbError) as caught:
            self.run_prepare(e2e, self.work / "handoff")
        self.assertEqual(caught.exception.reason, "metrics-cross-check")

    def test_a_comparison_without_reported_metrics_is_still_measured(self) -> None:
        # CompareMetrics ``reported`` is optional (ADAPTER.md); the cross-check compares it only
        # when the adapter supplies it, while reported_pixel stays mandatory.
        reported = ',\n                            "reported": report["pixel_validation"]["comparisons"]'
        silent = self.forked(lambda source: source.replace(
            reported + '[f"{first[\'step\']}->{second[\'step\']}"]})', "})"))
        producer = support.invocation(silent, self.env, implementation_sha=self.mod.commit)
        result = prepare.prepare_handoff(producer, e2e_root=self.e2e, key=KEY, output=self.work / "handoff",
                                         subject=self.mod.subject, tested=self.tested_claim, handoff=self.handoff_claim)
        self.assertEqual(result.manifest["comparisons"], self.prepared.manifest["comparisons"])
        validate.validate_handoff_dir(support.invocation(silent, self.env), self.work / "handoff", key=KEY)

    def test_a_comparison_that_did_not_change_enough_fails(self) -> None:
        e2e = self.copy(self.e2e, "e2e")
        for profile in e2e.glob("profiles/*--full"):
            screenshots = profile / "client_a/screenshots"
            shutil.copyfile(screenshots / "title_screen.png", screenshots / "skin_apply.png")

        def same_metrics(path: Path, result: dict) -> None:
            metrics = result["reports"]["client_a"]["pixel_validation"]["screenshots"]
            if "skin_apply" in metrics:
                metrics["skin_apply"] = metrics["title_screen"]

        self.rewrite_result(e2e, same_metrics)
        with self.assertRaisesRegex(ImageError, "did not change enough"):
            self.run_prepare(e2e, self.work / "handoff")

    def test_source_images_must_have_the_exact_source_size(self) -> None:
        e2e = self.copy(self.e2e, "e2e")
        path = next(e2e.glob("profiles/*--session/client_b/screenshots/player_list.png"))
        path.write_bytes(pattern_png(200, 100, 5))
        with self.assertRaises(ImageError):
            self.run_prepare(e2e, self.work / "handoff")

    def test_symlinked_runtime_files_are_refused(self) -> None:
        e2e = self.copy(self.e2e, "e2e")
        path = next(e2e.glob("profiles/*--session/client_b/screenshots/player_list.png"))
        target = self.work / "elsewhere.png"
        shutil.move(path, target)
        os.symlink(target, path)
        with self.assertRaises(MbError):
            self.run_prepare(e2e, self.work / "handoff")

    def test_a_failing_anchor_decision_leaves_nothing_behind(self) -> None:
        failing = self.forked(lambda source: source.replace(
            'def anchor_selection(ctx, expectation):\n    return expectation["anchor"]',
            'def anchor_selection(ctx, expectation):\n    raise ValueError("no anchor decision")'))
        producer = support.invocation(failing, self.env, implementation_sha=self.mod.commit)
        with self.assertRaisesRegex(ValueError, "no anchor decision"):
            prepare.prepare_handoff(producer, e2e_root=self.e2e, key=KEY, output=self.work / "handoff",
                                    subject=self.mod.subject, tested=self.tested_claim, handoff=self.handoff_claim)
        self.assertFalse((self.work / "handoff").exists())
        self.assertEqual([path.name for path in self.work.iterdir()], ["forked-repo"], "no stage is left either")

    def test_prepare_runs_every_hook_at_the_subject_commit(self) -> None:
        # A controller split: the producing run's GITHUB_SHA is not the subject commit and the
        # invocation carries no override; prepare and the composite's fresh-process validation
        # still hand the subject commit to the hooks (SPEC §4.2).
        controller = "c" * 40
        environ = support.environment(self.mod, run_id=4400, sha=controller)
        handoff, tested = support.claims(environ, subject=self.mod.subject)
        result = prepare.prepare_handoff(support.invocation(self.mod, environ), e2e_root=self.e2e, key=KEY,
                                         output=self.work / "split", subject=self.mod.subject, tested=tested,
                                         handoff=handoff)
        self.assertEqual(result.manifest["provenance"]["handoff"]["controller_sha"], controller)
        self.assertFalse(result.anchor_eligible)
        manifest = validate.validate_handoff_dir(support.invocation(self.mod, environ), self.work / "split", key=KEY,
                                                 expected_subject_commit=self.mod.commit)
        self.assertEqual(manifest, result.manifest)

    def test_anchor_output_is_only_a_reservation(self) -> None:
        with self.assertRaisesRegex(MbError, "requires anchor 'auto'"):
            self.run_prepare(self.e2e, self.work / "a", anchor="off", anchor_output=self.work / "anchor")
        with self.assertRaisesRegex(MbError, "outside"):
            self.run_prepare(self.e2e, self.work / "b", anchor_output=self.work / "b/anchor")
        result = self.run_prepare(self.e2e, self.work / "c", anchor="off")
        self.assertFalse(result.anchor_eligible)
        self.assertNotIn(("anchor_selection", False), self.host.calls)


class ValidateHandoffTest(Flow):
    def test_validation_in_the_producing_run_rederives_the_expectation(self) -> None:
        manifest = validate.validate_handoff_dir(support.invocation(self.mod, self.env), self.handoff, key=KEY,
                                                 expected_subject_commit=self.mod.commit)
        self.assertEqual(manifest, self.prepared.manifest)
        self.assertIn(("expectation", False), self.host.calls)
        self.assertIn(("collect", False), self.host.calls)

    def test_rederivation_outside_the_producing_run_needs_the_authenticated_event(self) -> None:
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(self.pages(), self.handoff, key=KEY)
        self.assertEqual(caught.exception.reason, "usage")
        validate.validate_handoff_dir(self.pages(), self.handoff, key=KEY, rederive=False)

    def assert_rejected(self, handoff: Path, **options: Any) -> MbError:
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(self.pages(), handoff, key=options.pop("key", KEY), rederive=False, **options)
        return caught.exception

    def test_tampered_bundles_are_rejected(self) -> None:
        png = self.prepared.manifest["frames"][0]["source"]["path"]
        cases = {
            "flipped byte": lambda root: _flip(root / png),
            "extra file": lambda root: (root / "runtime/extra.json").write_text("{}"),
            "missing file": lambda root: (root / png).unlink(),
            "non-canonical manifest": lambda root: (root / "manifest.json").write_text(
                json.dumps(json.loads((root / "manifest.json").read_bytes()), indent=1)),
            "non-canonical expectation": lambda root: _pretty(root / "expectation.json"),
            "symlinked file": lambda root: _symlink_in_place(root / png),
            "forged pixel record": lambda root: _edit_manifest(
                root, lambda manifest: manifest["frames"][0]["source"]["pixel"].update(luma_entropy=1.0)),
        }
        for label, change in cases.items():
            with self.subTest(label):
                root = self.copy(self.handoff, label.replace(" ", "-"))
                change(root)
                self.assert_rejected(root)

    def test_identity_checks(self) -> None:
        self.assert_rejected(self.handoff, expected_subject_commit="f" * 40)
        self.assert_rejected(self.handoff, key="mc26.3")
        other = support.invocation(self.mod, {**support.pages_environment(self.mod),
                                              "GITHUB_REPOSITORY": "The-Plum-Team/other"})
        with self.assertRaisesRegex(MbError, "another repository"):
            validate.validate_handoff_dir(other, self.handoff, key=KEY, rederive=False)

    def test_a_hostile_manifest_is_a_rejection_before_any_claim_is_read(self) -> None:
        root = self.work / "hostile"
        root.mkdir()
        (root / "manifest.json").write_bytes(canonical_json({"kind": "mod-base.evidence.handoff",
                                                             "provenance": {"handoff": {}}}))
        with self.assertRaises(MbError):
            validate.validate_handoff_dir(support.invocation(self.mod, self.env), root, key=KEY)

    def test_consumers_apply_the_configured_reuse_rules(self) -> None:
        tested = {**self.tested_claim, "run_id": 4000, "controller_sha": self.mod.commit}
        extensions = self.work / "extensions.json"
        reference = {"repository": self.mod.repository, "run_id": 4000, "tested_sha": self.mod.commit}
        extensions.write_text(json.dumps({"quick-skin.runtime_source": reference}))
        delegated = self.run_prepare(self.e2e, self.work / "delegated", tested=tested, extensions_path=extensions)
        self.assertEqual(delegated.manifest["provenance"]["reuse"], "delegated")
        validate.validate_handoff_dir(self.pages(), self.work / "delegated", key=KEY, rederive=False)
        tested_run = support.run_record(tested, event="workflow_dispatch", head_sha="d" * 40)
        self.compact(self.work / "delegated", tested_run=tested_run)
        validate.validate_compact_dir(self.pages(), self.work / "compact", key=KEY)
        pages = support.pages_environment(self.mod)
        for source in ({"delegated_reuse_extension": None},
                       {"delegated_reuse_extension": "quick-skin.feature_selection"}):
            invocation = build_invocation(self.mod.root, self.with_source_config(**source), pages)
            checks = {"handoff": lambda: validate.validate_handoff_dir(invocation, self.work / "delegated", key=KEY,
                                                                       rederive=False),
                      "compact": lambda: validate.validate_compact_dir(invocation, self.work / "compact", key=KEY)}
            for label, check in checks.items():
                with self.subTest(source=source, bundle=label), self.assertRaises(MbError) as caught:
                    check()
                self.assertEqual(caught.exception.reason, "reuse")

    def test_validate_bundle_dispatch(self) -> None:
        with self.assertRaisesRegex(MbError, "--bind-raw"):
            validate.validate_bundle(self.pages(), "handoff", self.handoff, key=KEY, bind_raw=self.handoff)
        with self.assertRaises(MbError):
            validate.validate_bundle(self.pages(), "unknown", self.handoff, key=KEY)
        with self.assertRaises(MbError):
            validate.validate_bundle(self.pages(), "compact", self.handoff, key=KEY)


class CompactTest(Flow):
    def test_compact_embeds_the_final_selection_and_binds_to_the_raw_pngs(self) -> None:
        manifest = self.compact(self.handoff)
        root = self.work / "compact"
        expectation = json.loads((root / "expectation.json").read_bytes())
        selection = json.loads((root / "selection.json").read_bytes())
        validate_compact(manifest, expectation=expectation)
        validate_selection(selection)
        check_compact_selection(manifest, selection)
        self.assertEqual(selection["binding"], {"mode": "reencode-identical", "frames": 8,
                                                "derivatives": len({f["derivative"]["path"] for f in manifest["frames"]})})
        self.assertEqual(manifest["kit"], selection["kit"])
        self.assertEqual(manifest["source_artifact"]["kind"], "handoff")
        self.assertEqual(manifest["scope"], {"kind": "complete"})
        self.assertEqual((root / "expectation.json").read_bytes(), (self.handoff / "expectation.json").read_bytes())
        self.assertEqual({record["path"] for record in manifest["files"]},
                         {"expectation.json", "selection.json", *(f["derivative"]["path"] for f in manifest["frames"])})
        for frame in manifest["frames"]:
            self.assertNotIn("path", frame["source"])
            self.assertEqual((frame["derivative"]["width"], frame["derivative"]["height"]),
                             thumbnail_size((192, 108), (160, 90)))
            raw = (self.handoff / next(item["source"]["path"] for item in self.prepared.manifest["frames"]
                                       if item["frame_id"] == frame["frame_id"])).read_bytes()
            self.assertEqual((root / frame["derivative"]["path"]).read_bytes(),
                             derive_webp(raw, box=[160, 90], quality=82, method=6))
        validate.validate_compact_dir(self.pages(), root, key=KEY, bind_raw=self.handoff,
                                      expected_subject_commit=self.mod.commit)
        validate.validate_bundle(self.pages(), "compact", root, key=KEY, bind_raw=self.handoff)

    def test_compaction_is_deterministic(self) -> None:
        first = self.compact(self.handoff, "first")
        second = self.compact(self.handoff, "second")
        self.assertEqual(canonical_json(first), canonical_json(second))
        for record in first["files"]:
            self.assertEqual((self.work / "first" / record["path"]).read_bytes(),
                             (self.work / "second" / record["path"]).read_bytes())

    def test_bind_raw_requires_byte_identical_reencoding(self) -> None:
        def other_encoder(source, *, box, quality, method):
            return derive_webp(source, box=box, quality=quality - 1, method=method)

        with mock.patch.object(compact_module, "derive_webp", other_encoder):
            self.compact(self.handoff)
        root = self.work / "compact"
        validate.validate_compact_dir(self.pages(), root, key=KEY)
        with self.assertRaises(MbError) as caught:
            validate.validate_compact_dir(self.pages(), root, key=KEY, bind_raw=self.handoff)
        self.assertEqual(caught.exception.reason, "reencode-mismatch")

    def test_bind_raw_refuses_another_raw_handoff(self) -> None:
        self.compact(self.handoff)
        other_env = support.environment(self.mod, run_id=5555)
        other_claim, other_tested = support.claims(other_env, subject=self.mod.subject)
        other = self.run_prepare(self.e2e, self.work / "other", handoff=other_claim, tested=other_tested)
        self.assertEqual(other.manifest["provenance"]["handoff"]["run_id"], 5555)
        with self.assertRaisesRegex(MbError, "raw handoff's upload"):
            validate.validate_compact_dir(self.pages(), self.work / "compact", key=KEY, bind_raw=self.work / "other")

    def test_draft_must_describe_the_selected_bundle(self) -> None:
        draft = json.loads(self.draft_for(self.handoff).read_bytes())
        cases = {
            "manifest hash": {"source_manifest_sha256": "0" * 64},
            "verified extensions": {"extensions_verified": ["quick-skin.runtime_source"]},
            "tested run": {"source": {**draft["source"],
                                      "tested_run": {**draft["source"]["tested_run"], "workflow_path":
                                                     ".github/workflows/other.yml"}}},
        }
        for label, change in cases.items():
            with self.subTest(label):
                path = self.work / f"{label.replace(' ', '-')}.json"
                path.write_bytes(canonical_json({**draft, **change}))
                with self.assertRaises(MbError):
                    compact_module.compact_bundle(self.pages(), key=KEY, input_dir=self.handoff, selection_path=path,
                                                  output=self.work / f"out-{label.replace(' ', '-')}")
        foreign_kit = support.pages_environment(self.mod)
        foreign_kit["MOD_BASE_KIT_SHA"] = "e" * 40
        with self.assertRaisesRegex(MbError, "another kit"):
            compact_module.compact_bundle(support.invocation(self.mod, foreign_kit), key=KEY, input_dir=self.handoff,
                                          selection_path=self.draft_for(self.handoff), output=self.work / "kit")

    def test_selected_handoff_is_never_compacted_alone(self) -> None:
        extensions = self.work / "extensions.json"
        extensions.write_text(json.dumps({"quick-skin.feature_selection": {"scenarios": ["full"],
                                                                           "baseline_artifact_id": 501}}))
        selected = self.run_prepare(self.e2e, self.work / "selected", extensions_path=extensions)
        self.assertEqual(selected.manifest["scope"]["kind"], "selected")
        with self.assertRaises(MbError) as caught:
            self.compact(self.work / "selected")
        self.assertEqual(caught.exception.reason, "selected-needs-compose")

    def test_cache_is_revalidated_and_reemitted_with_its_new_selection(self) -> None:
        first = self.compact(self.handoff, "first")
        cache = self.work / "first"
        reemitted = self.compact(cache, "second", kind="cache", owner_run_id=8100, artifact_id=77,
                                 digest="sha256:" + "c" * 64, size=999)
        selection = json.loads((self.work / "second/selection.json").read_bytes())
        self.assertEqual(selection["binding"]["mode"], "cache-revalidated")
        self.assertEqual(reemitted["source_artifact"]["kind"], "cache")
        self.assertEqual(reemitted["source_artifact"]["id"], 77)
        self.assertEqual(reemitted["frames"], first["frames"])
        for frame in first["frames"]:
            path = frame["derivative"]["path"]
            self.assertEqual((self.work / "second" / path).read_bytes(), (cache / path).read_bytes())
        validate.validate_compact_dir(self.pages(), self.work / "second", key=KEY)

    def test_compact_validation_rejects_tampering(self) -> None:
        self.compact(self.handoff)
        manifest = json.loads((self.work / "compact/manifest.json").read_bytes())
        image = manifest["frames"][0]["derivative"]["path"]
        cases = {
            "flipped derivative": lambda root: _flip(root / image),
            "non-canonical selection": lambda root: _pretty(root / "selection.json"),
            "stale selection identity": lambda root: _edit_selection(root, manifest_sha256="0" * 64),
            "forged derivative metrics": lambda root: _edit_manifest(
                root, lambda value: [frame["derivative"]["pixel"].update(dark_fraction=0.5)
                                     for frame in value["frames"] if frame["derivative"]["path"] == image]),
        }
        for label, change in cases.items():
            with self.subTest(label):
                root = self.copy(self.work / "compact", label.replace(" ", "-"))
                change(root)
                with self.assertRaises(MbError):
                    validate.validate_compact_dir(self.pages(), root, key=KEY)


class CommandLineTest(Flow):
    """The production path: ``python3 -P -m mod_base`` with the real isolated hook host."""

    def run_kit(self, *arguments: str, environ: dict[str, str]) -> subprocess.CompletedProcess:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(Path(KIT) / "src"),
                       "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                       "HOME": str(self.work), **environ}
        return subprocess.run([sys.executable, "-P", "-m", "mod_base", *arguments], env=environment,
                              capture_output=True, text=True, timeout=300, check=False)

    def test_prepare_and_validate_through_the_isolated_host(self) -> None:
        output = self.work / "github_output"
        environ = {**self.env, "GITHUB_OUTPUT": str(output)}
        completed = self.run_kit(
            "prepare", "--repo", str(self.mod.root), "--e2e-root", str(self.e2e), "--key", KEY,
            "--output", str(self.work / "handoff"), "--subject-branch", "master", "--subject-commit", self.mod.commit,
            "--subject-tree", self.mod.tree, "--tested-run-id", "4242", "--tested-run-attempt", "1",
            "--tested-branch", "master", "--tested-commit", self.mod.commit, "--tested-controller-branch", "master",
            "--tested-controller-sha", self.mod.commit, "--anchor", "auto", environ=environ)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output.read_text(), "anchor_eligible=true\n")
        self.assertEqual((self.work / "handoff/manifest.json").read_bytes(), (self.handoff / "manifest.json").read_bytes())
        self.assertEqual(self.host.calls, [], "the command line never used the in-process seam")
        validate_arguments = ("validate", "--repo", str(self.mod.root), "--key", KEY, "--kind", "handoff",
                              "--input", str(self.work / "handoff"), "--expected-subject-commit", self.mod.commit)
        completed = self.run_kit(*validate_arguments, environ=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        _flip(self.work / "handoff" / self.prepared.manifest["frames"][0]["source"]["path"])
        completed = self.run_kit(*validate_arguments, environ=self.env)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(len(completed.stderr.strip().splitlines()), 1)


    def test_a_hostile_handoff_manifest_exits_2(self) -> None:
        root = self.work / "hostile"
        root.mkdir()
        (root / "manifest.json").write_bytes(canonical_json({"kind": "mod-base.evidence.handoff",
                                                             "provenance": {"handoff": {}}}))
        completed = self.run_kit("validate", "--repo", str(self.mod.root), "--key", KEY, "--kind", "handoff",
                                 "--input", str(root), environ=self.env)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(len(completed.stderr.strip().splitlines()), 1)

    def test_expect_compact_and_bind_raw_through_the_isolated_host(self) -> None:
        completed = self.run_kit("expect", "--repo", str(self.mod.root), "--key", KEY,
                                 "--output", str(self.work / "expectation.json"), environ=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.work / "expectation.json").read_bytes(), (self.handoff / "expectation.json").read_bytes())
        pages = support.pages_environment(self.mod, token=None)
        completed = self.run_kit("compact", "--repo", str(self.mod.root), "--key", KEY, "--input", str(self.handoff),
                                 "--selection", str(self.draft_for(self.handoff)), "--output", str(self.work / "compact"),
                                 environ=pages)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        arguments = ("validate", "--repo", str(self.mod.root), "--key", KEY, "--kind", "compact",
                     "--input", str(self.work / "compact"), "--bind-raw", str(self.handoff))
        completed = self.run_kit(*arguments, environ={**pages, "GITHUB_JOB": "refresh"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.host.calls, [])


class BlockPopsTest(Flow):
    fixture = "bp_like"
    event = "schedule"

    def test_enrolled_branch_scheduled_projection_and_anchor(self) -> None:
        manifest = self.prepared.manifest
        self.assertEqual(manifest["key"], hashlib.sha256(b"master").hexdigest()[:24])
        self.assertEqual({lane["profile"] for lane in manifest["lanes"]}, {"scheduled-anchors"})
        self.assertEqual({lane["jars"]["harness_sha256"] != "" for lane in manifest["lanes"]}, {True})
        self.assertEqual(len(manifest["comparisons"]), 2)
        self.assertEqual(manifest["scope"]["kind"], "complete")
        self.assertIn("detail_sha256", manifest["scope"])
        self.assertTrue(self.prepared.anchor_eligible)
        validate.validate_handoff_dir(support.invocation(self.mod, self.env), self.handoff, key=self.key)

    def test_attested_reuse_keeps_the_tested_run(self) -> None:
        tested = {**self.tested_claim, "run_id": 4000, "run_attempt": 2}
        result = self.run_prepare(self.e2e, self.work / "attested", tested=tested)
        self.assertEqual(result.manifest["provenance"]["reuse"], "attested")
        self.assertEqual(result.manifest["provenance"]["tested"]["run_id"], 4000)
        self.assertFalse(result.anchor_eligible, "an attestation never cuts an anchor")
        validate.validate_handoff_dir(self.pages(), self.work / "attested", key=self.key, rederive=False)
        unattested = build_invocation(self.mod.root, self.with_source_config(attestation_job=None),
                                      support.pages_environment(self.mod))
        with self.assertRaises(MbError) as caught:
            validate.validate_handoff_dir(unattested, self.work / "attested", key=self.key, rederive=False)
        self.assertEqual(caught.exception.reason, "reuse")

    def test_unenrolled_branches_have_no_target(self) -> None:
        matrix = json.loads((self.mod.root / "release/release-matrix.json").read_text())
        matrix["branch"]["name"] = "master"
        feature = support.commit_on_branch(self.mod, "feature", {
            "release/release-matrix.json": json.dumps(matrix).encode()})
        without = support.commit_on_branch(self.mod, "no-matrix", {"release/release-matrix.json": None})
        for head in (feature, without):
            key = hashlib.sha256(head.branch.encode()).hexdigest()[:24]
            with self.subTest(head.branch), self.assertRaisesRegex(Unavailable, f"no target for branch {head.branch}"):
                # A lone unenrolled subject leaves the adapter no valid targets result (1..max): the
                # documented "missing key", exit 3.
                expectation_module.target_for_key(self.producer(), key, subject=head.subject)
        with mock.patch.object(host, "call", REAL_CALL), self.assertRaises(Unavailable) as caught:
            expectation_module.target_for_key(self.producer(), hashlib.sha256(b"feature").hexdigest()[:24],
                                              subject=feature.subject)
        self.assertEqual(caught.exception.exit_code, 3, "the same answer across the isolated host")
        with self.assertRaises(Unavailable):
            expectation_module.target_for_key(self.producer(), "0" * 24, subject=self.mod.subject)

    def test_any_other_targets_failure_stays_a_rejection(self) -> None:
        broken = self.forked(lambda source: source.replace(
            '    if branches is None:\n        raise ValueError("the bp-like fixture uses enrolled-branches mode")',
            '    raise ValueError("the matrix reader is broken")'))
        with mock.patch.object(host, "call", REAL_CALL), self.assertRaises(MbError) as caught:
            expectation_module.target_for_key(support.invocation(broken, self.env), self.key, subject=self.mod.subject)
        self.assertNotIsInstance(caught.exception, Unavailable)
        self.assertIn("the matrix reader is broken", str(caught.exception))

    def test_bp_compact_round_trip(self) -> None:
        self.compact(self.handoff, event="schedule")
        validate.validate_compact_dir(self.pages(), self.work / "compact", key=self.key, bind_raw=self.handoff)


def _flip(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


def _pretty(path: Path) -> None:
    path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2))


def _symlink_in_place(path: Path) -> None:
    moved = path.with_name(path.name + ".real")
    shutil.move(path, moved)
    os.symlink(moved.name, path)


def _edit_manifest(root: Path, change) -> None:
    manifest = json.loads((root / "manifest.json").read_bytes())
    change(manifest)
    (root / "manifest.json").write_bytes(canonical_json(manifest))


def _edit_selection(root: Path, **fields: Any) -> None:
    selection = json.loads((root / "selection.json").read_bytes())
    selection.update(fields)
    data = canonical_json(selection)
    (root / "selection.json").write_bytes(data)

    def rebind(manifest: dict) -> None:
        record = {"path": "selection.json", "sha256": sha256(data), "size": len(data)}
        manifest["selection"] = record
        manifest["files"] = [record if item["path"] == "selection.json" else item for item in manifest["files"]]

    _edit_manifest(root, rebind)


if __name__ == "__main__":
    unittest.main()
