"""``family collect`` (MB4): the envelope, the recorded selection and its kit binding, the adapter's
``family_validate``, R4/R5, the collected layout and the exit-3 statuses that never produce an upload."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.family import paired
from mod_base.family.envelope import ENVELOPE_NAME, create_envelope, validate_envelope_dir
from mod_base.family.paired import (
    IMAGES_DIRECTORY,
    PROJECTION_NAME,
    SELECTED_NAME,
    SOURCE_DIRECTORY,
    FamilyOutcome,
    collect_family,
    validate_projection,
)
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.runtime import Invocation
from tests import test_family_support as fs
from tests.fixtures.mods import support
from tests.test_family_envelope import deepest_native_path, listing, longest_native_path, with_family

#: The production isolated-child host (tests replace ``host.call`` with the in-process one).
CHILD_HOST_CALL = host.call


class CollectTest(unittest.TestCase):
    """History: C0 <- C1 on ``master`` (checked out at C1) and S forked from C0 on ``side``.
    ``handoff(subject, coverage)`` envelopes a native bundle produced at ``subject`` (C0 or C1)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="mb-family-collect-")).resolve()
        cls.c0 = fs.family_mod(cls.root / "mod")
        cls.c1 = fs.advance(cls.c0, "c1.txt")
        cls.s = fs.advance(cls.c1, "s.txt", branch="side", start=cls.c0.commit)
        cls.invocation = fs.family_invocation(cls.c1)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))
        self.host = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)

    def handoff(self, *, subject: support.FixtureMod | None = None, **native: Any) -> Path:
        """Envelope a native bundle produced by the run at ``subject`` (default C1, the checkout),
        which covers exactly that commit."""

        producer = subject or self.c1
        work = Path(tempfile.mkdtemp(prefix="handoff-", dir=self.work))
        bundle = fs.native_bundle(work / "bundle", producer, **native)
        support.git(self.c1.root, "checkout", "-q", "--detach", producer.commit)
        try:
            create_envelope(fs.producer_invocation(producer), family=fs.FAMILY, key=fs.KEY, bundle_dir=bundle,
                            coverage_sha=producer.commit,
                            subject={"branch": producer.branch, "commit": producer.commit},
                            producer=fs.producer_claim(producer), output=work / "handoff")
        finally:
            support.git(self.c1.root, "checkout", "-q", self.c1.branch)
        return work / "handoff"

    def selected_json(self, value: Any) -> Path:
        """``value`` written as ``select --output`` writes it, into a new file outside every input."""

        return fs.write_selection(Path(tempfile.mkdtemp(prefix="selected-", dir=self.work)) / "selected.json", value)

    def collect(self, input_dir: Path, *, expected: str | None = None, output: Path | None = None,
                invocation: Invocation | None = None, selected: Any = None) -> FamilyOutcome:
        """``family collect`` of ``input_dir`` with the recorded selection ``selected`` (by default the
        family handoff of its envelope's producer attempt, :func:`fs.selection`)."""

        self.selection = fs.selection(input_dir) if selected is None else selected
        return collect_family(invocation or self.invocation, family=fs.FAMILY, key=fs.KEY, input_dir=input_dir,
                              expected_coverage_sha=expected or self.c1.commit,
                              output=output or self.work / "collected", selected_json=self.selected_json(self.selection))

    def assert_rejected(self, action: Any, fragment: str = "", reason: str | None = None) -> MbError:
        with self.assertRaises(MbError) as caught:
            action()
        self.assertIn(fragment, str(caught.exception))
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertFalse((self.work / "collected").exists(), "a rejected collection writes nothing")
        return caught.exception

    def assert_collected(self, handoff: Path, outcome: FamilyOutcome, expected: str) -> None:
        output = self.work / "collected"
        self.assertEqual(outcome.status, "available")
        self.assertEqual(outcome.projection["coverage_sha"], expected)
        written = listing(output)
        self.assertEqual(written[PROJECTION_NAME], canonical_json(outcome.projection))
        self.assertEqual({name[len(SOURCE_DIRECTORY) + 1:]: data for name, data in written.items()
                          if name.startswith(SOURCE_DIRECTORY + "/")}, listing(handoff))
        images = {name for name in written if name.startswith(IMAGES_DIRECTORY + "/")}
        self.assertEqual(set(written), {PROJECTION_NAME, SELECTED_NAME} | images | {f"{SOURCE_DIRECTORY}/{name}"
                                                                                    for name in listing(handoff)})
        self.assertEqual(written[SELECTED_NAME], canonical_json(self.selection), "the recorded selection, verbatim")
        self.assertEqual(validate_projection(self.invocation, output / PROJECTION_NAME, images_root=output,
                                             family=fs.FAMILY, key=fs.KEY, expected_coverage_sha=expected),
                         outcome.projection)
        validate_envelope_dir(self.invocation, output / SOURCE_DIRECTORY, family=fs.FAMILY, key=fs.KEY)


class AvailableTest(CollectTest):
    def test_a_current_generation_is_collected_verbatim(self) -> None:
        handoff = self.handoff()
        outcome = self.collect(handoff)
        self.assertEqual((outcome.reason, outcome.carried_from), ("fixture projection", None))
        self.assert_collected(handoff, outcome, self.c1.commit)
        self.assertEqual(self.host.calls, [("family_validate", False)])

    def test_the_collected_source_stays_re_collectable(self) -> None:
        handoff = self.handoff()
        self.collect(handoff)
        cache = fs.copy_tree(self.work / "collected" / SOURCE_DIRECTORY, self.work / "cache")
        again = self.collect(cache, output=self.work / "again")
        self.assertEqual(again.projection, self.collect(handoff, output=self.work / "third").projection)

    def test_a_projection_may_live_in_a_subdirectory(self) -> None:
        handoff = self.handoff(adjust=lambda manifest: manifest.update(projection_path="view/paired.json"))
        self.assert_collected(handoff, self.collect(handoff), self.c1.commit)

    def test_nested_native_bundles_are_collected(self) -> None:
        nested = {"evidence/lanes/1.20.1-fabric/ears/proof.json": b"{}", deepest_native_path(): b"{}",
                  longest_native_path(): b"{}"}
        cases = {
            "with lanes": {"extra": nested},
            "without lanes": {"extra": nested, "images": {},
                              "projection_adjust": lambda projection: projection.update(lanes=[])},
        }
        for label, native in cases.items():
            with self.subTest(label=label):
                handoff = self.handoff(**native)
                output = Path(tempfile.mkdtemp(prefix="nested-", dir=self.work)) / "collected"
                outcome = self.collect(handoff, output=output)
                self.assertEqual(outcome.status, "available")
                written = listing(output)
                for path, data in nested.items():
                    self.assertEqual(written[f"{SOURCE_DIRECTORY}/{path}"], data)
                validate_envelope_dir(self.invocation, output / SOURCE_DIRECTORY, family=fs.FAMILY, key=fs.KEY)

    def test_the_isolated_child_host_collects_the_same_projection(self) -> None:
        handoff = self.handoff()
        in_process = self.collect(handoff, output=self.work / "in-process")
        with mock.patch.object(host, "call", wraps=CHILD_HOST_CALL) as call:
            child = self.collect(handoff)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(child.projection, in_process.projection)
        self.assert_collected(handoff, child, self.c1.commit)


class CarryForwardTest(CollectTest):
    def test_an_ancestor_generation_is_carried_forward_and_re_proven(self) -> None:
        handoff = self.handoff(subject=self.c0, adjust=lambda manifest: manifest.update(carry=True))
        with mock.patch.object(paired, "verify_carry_forward", wraps=paired.verify_carry_forward) as r5:
            outcome = self.collect(handoff)
        self.assertEqual(outcome.carried_from, self.c0.commit)
        r5.assert_called_once_with(self.invocation.repo_root, self.c0.commit, self.c1.commit)
        self.assert_collected(handoff, outcome, self.c1.commit)
        self.assertEqual(json.loads((self.work / "collected" / SOURCE_DIRECTORY / ENVELOPE_NAME).read_bytes())
                         ["coverage_sha"], self.c0.commit, "the source is the selected generation verbatim")

    def test_the_adapter_can_refuse_a_carry_forward(self) -> None:
        outcome = self.collect(self.handoff(subject=self.c0))
        self.assertEqual(outcome, FamilyOutcome(status="unavailable", reason="fixture lineage refusal"))

    def test_a_carry_forward_across_non_ancestors_is_rejected(self) -> None:
        handoff = self.handoff(adjust=lambda manifest: manifest.update(carry=True))
        self.assert_rejected(lambda: self.collect(handoff, expected=self.s.commit), "not an ancestor", "carry-forward")

    def test_a_carry_forward_needs_the_family_to_allow_it(self) -> None:
        handoff = self.handoff(subject=self.c0, adjust=lambda manifest: manifest.update(carry=True))
        self.assert_rejected(lambda: self.collect(handoff, invocation=with_family(self.invocation,
                                                                                  carry_forward=False)),
                             "does not allow carry-forward", "carry-forward")

    def test_the_hook_must_carry_from_the_envelope_coverage(self) -> None:
        cases = {
            "another origin": (self.handoff(adjust=lambda manifest: manifest.update(
                carry=True, carried_from=self.c0.commit)), self.s.commit, "not from the envelope coverage"),
            "an origin for current evidence": (self.handoff(adjust=lambda manifest: manifest.update(
                carried_from=self.c0.commit)), self.c1.commit, "not from the envelope coverage"),
            "no origin for stale evidence": (self.handoff(subject=self.c0, adjust=lambda manifest: manifest.update(
                coverage_sha=self.c1.commit)), self.c1.commit, "reported no carry-forward"),
        }
        for label, (handoff, expected, fragment) in cases.items():
            with self.subTest(label=label):
                self.assert_rejected(lambda: self.collect(handoff, expected=expected), fragment, "carry-forward")

    def test_missing_ancestry_objects_are_rejected(self) -> None:
        handoff = self.handoff(adjust=lambda manifest: manifest.update(carry=True))
        self.assert_rejected(lambda: self.collect(handoff, expected="9" * 40), "not present as an inert object", "git")

    def test_an_envelope_covers_only_its_producer_checkout(self) -> None:
        handoff = self.handoff(adjust=lambda manifest: manifest.update(coverage_sha=self.s.commit))
        fs.rewrite_envelope(handoff, lambda envelope: envelope.update(coverage_sha=self.s.commit))
        self.assert_rejected(lambda: self.collect(handoff, expected=self.s.commit), "not its producer's checkout")
        self.assertEqual(self.host.calls, [], "a forged coverage never reaches the hook")

    def test_git_is_never_taken_from_the_inherited_path(self) -> None:
        handoff = self.handoff(subject=self.c0, adjust=lambda manifest: manifest.update(carry=True))
        empty = Path(tempfile.mkdtemp(prefix="no-git-", dir=self.work))
        with mock.patch("mod_base.family._git._GIT_SEARCH_PATH", str(empty)):
            self.assert_rejected(lambda: self.collect(handoff), "git is not installed", "git")

    def test_an_envelope_carry_forward_is_re_proven(self) -> None:
        handoff = self.handoff()
        fs.rewrite_envelope(handoff, lambda envelope: envelope.update(carried_from=self.c0.commit))
        self.assertEqual(self.collect(handoff, output=self.work / "proven").status, "available")
        fs.rewrite_envelope(handoff, lambda envelope: envelope.update(carried_from=self.s.commit))
        self.assert_rejected(lambda: self.collect(handoff), "not an ancestor", "carry-forward")
        self.assertEqual(self.host.calls, [("family_validate", False)], "a forged envelope never reaches the hook")


class AbsentTest(CollectTest):
    def test_superseded_and_unavailable_are_returned_without_output(self) -> None:
        for status in ("superseded", "unavailable"):
            with self.subTest(status=status):
                outcome = self.collect(self.handoff(adjust=lambda manifest: manifest.update(outcome=status)))
                self.assertEqual(outcome, FamilyOutcome(status=status, reason=f"fixture {status}"))
                self.assertFalse((self.work / "collected").exists())


class RejectedTest(CollectTest):
    def test_the_projection_is_re_verified(self) -> None:
        def pair(field: str, value: Any) -> Any:
            return lambda projection: projection["lanes"][0]["pairs"][0]["verdict"].update({field: value})

        cases = {
            "unclean verdict": (pair("defect", True), "not publishable"),
            "other subject": (lambda projection: projection["subject"].update(tree="1" * 40), "subject"),
            "other producer": (lambda projection: projection["provenance"]["producer"].update(run_attempt=2),
                               "producer"),
            "other key": (lambda projection: projection.update(key="mc26.3"), "key"),
            "unconfigured producer event": (lambda projection: projection["provenance"]["producer"].update(
                event="push"), "producer event"),
            "unknown member": (lambda projection: projection.update(note="x"), "note"),
            "other image policy": (lambda projection: projection["image_policy"].update(webp_method=4),
                                   "image policy"),
            "metric drift": (lambda projection: projection["lanes"][0]["pairs"][0]["candidate"]["image"]["pixel"]
                             .update(light_fraction=0.5), "pixel metrics"),
        }
        for label, (change, fragment) in cases.items():
            with self.subTest(label=label):
                handoff = self.handoff(projection_adjust=change)
                self.assert_rejected(lambda: self.collect(handoff), fragment)

    def test_the_hook_writes_exactly_its_projection_and_images(self) -> None:
        cases = {
            "a stray file": ({"notes.txt": "x"}, "wrote files beside its projection"),
            "a stray image": ({"images/stray.webp": "x"}, "images/ differs from the projection"),
            "a stray nested file": ({"view/x.json": "{}"}, "wrote files beside its projection"),
        }
        for label, (extra, fragment) in cases.items():
            with self.subTest(label=label):
                handoff = self.handoff(adjust=lambda manifest: manifest.update(extra_files=extra))
                self.assert_rejected(lambda: self.collect(handoff), fragment)

    def test_the_hook_output_holds_no_link_or_special_file(self) -> None:
        cases = {
            "a symlink": ({"kind": "symlink", "path": "link.json"}, "symlink"),
            "a symlinked image": ({"kind": "symlink", "path": "images/link.webp"}, "symlink"),
            "a hard link": ({"kind": "hardlink", "path": "hard.json"}, "hard-linked"),
            "a FIFO": ({"kind": "fifo", "path": "pipe"}, "special file"),
        }
        for label, (special, fragment) in cases.items():
            with self.subTest(label=label):
                handoff = self.handoff(adjust=lambda manifest: manifest.update(specials=[special]))
                self.assert_rejected(lambda: self.collect(handoff), fragment, "unsafe-tree")

    def test_an_image_swapped_after_re_inspection_is_caught(self) -> None:
        handoff = self.handoff()
        reinspect = paired._reinspect

        def swap_after(root: Path, path: str, image: Any) -> None:
            reinspect(root, path, image)
            root.joinpath(*path.split("/")).write_bytes(b"swapped after re-inspection")

        with mock.patch.object(paired, "_reinspect", side_effect=swap_after) as inspected:
            self.assert_rejected(lambda: self.collect(handoff), "differs from its recorded size or SHA-256")
        self.assertEqual(inspected.call_count, 2)

    def test_the_collected_output_must_fit_its_extraction_bounds(self) -> None:
        handoff = self.handoff()
        files = len(listing(handoff)) + 2 + 2  # source/, paired.json and selected.json, two images
        size = sum(len(data) for data in listing(handoff).values())
        bound = LIMITS_BY_KIND["collected-family"]
        for label, limits in {"entries": dataclasses.replace(bound, max_entries=files - 1),
                              "bytes": dataclasses.replace(bound, max_total_bytes=size)}.items():
            with self.subTest(label=label):
                with mock.patch.dict(LIMITS_BY_KIND, {"collected-family": limits}):
                    self.assert_rejected(lambda: self.collect(handoff), "extraction bounds")

    def test_a_projection_the_hook_did_not_write_is_rejected(self) -> None:
        handoff = self.handoff()
        with mock.patch.object(host, "call", return_value={"status": "available", "reason": "lying",
                                                           "projection_path": "absent.json"}):
            self.assert_rejected(lambda: self.collect(handoff), "no such projection")

    def test_a_hook_that_tampers_with_the_selected_generation_is_caught(self) -> None:
        handoff = self.handoff(adjust=lambda manifest: manifest.update(tamper=["manifest.json"]))
        self.assert_rejected(lambda: self.collect(handoff), "differs from its recorded size or SHA-256")

    def test_an_invalid_envelope_never_reaches_the_hook(self) -> None:
        handoff = self.handoff()
        (handoff / "stray.json").write_text("{}", encoding="utf-8")
        self.assert_rejected(lambda: self.collect(handoff), "inventory differs")
        self.assertEqual(self.host.calls, [])
        other = self.handoff()
        self.assert_rejected(lambda: collect_family(self.invocation, family=fs.FAMILY, key="mc26.3", input_dir=other,
                                                    expected_coverage_sha=self.c1.commit,
                                                    output=self.work / "collected",
                                                    selected_json=self.selected_json(fs.selection(other))), "key")

    def test_the_hook_runs_only_in_the_family_job(self) -> None:
        handoff = self.handoff()
        for job in ("collect", "build", "finalize", "refresh-family"):
            with self.subTest(job=job):
                invocation = dataclasses.replace(self.invocation, environ={**self.invocation.environ,
                                                                           "GITHUB_JOB": job})
                self.assert_rejected(lambda: self.collect(handoff, invocation=invocation), "family_validate")

    def test_an_adapter_without_the_hook_fails_closed(self) -> None:
        mod = fs.family_mod(self.work / "hookless", hook=False)
        invocation = fs.family_invocation(mod)
        bundle = fs.native_bundle(self.work / "bundle", mod)
        create_envelope(fs.producer_invocation(mod), family=fs.FAMILY, key=fs.KEY, bundle_dir=bundle,
                        coverage_sha=mod.commit, subject={"branch": mod.branch, "commit": mod.commit},
                        producer=fs.producer_claim(mod), output=self.work / "handoff")
        self.assert_rejected(lambda: collect_family(invocation, family=fs.FAMILY, key=fs.KEY,
                                                    input_dir=self.work / "handoff",
                                                    expected_coverage_sha=mod.commit,
                                                    output=self.work / "collected",
                                                    selected_json=self.selected_json(fs.selection(self.work / "handoff"))),
                             "family_validate", "hook-unsupported")

    def test_the_output_is_new_and_outside_the_input(self) -> None:
        handoff = self.handoff()
        (self.work / "collected").mkdir()
        with self.assertRaises(MbError):
            self.collect(handoff)
        (self.work / "collected").rmdir()
        self.assert_rejected(lambda: self.collect(handoff, output=handoff / "collected"), "outside")
        self.assert_rejected(lambda: self.collect(handoff, output=handoff), "outside")


class SelectionTest(CollectTest):
    """The recorded selection (``select --family --output``) is bound to the collected generation
    before the adapter runs, written verbatim into the output, and the generation's kit is the pin of
    its producer workflow at the producer commit (SPEC §1.8), read from the inert object store."""

    def test_a_carried_family_cache_is_named_between_its_envelope_and_the_coverage(self) -> None:
        handoff = self.handoff(subject=self.c0, adjust=lambda manifest: manifest.update(carry=True))
        for label, named in (("named by its envelope's coverage", self.c0.commit),
                             ("carried to the coverage by an earlier publication", self.c1.commit)):
            with self.subTest(label=label):
                output = Path(tempfile.mkdtemp(prefix="cache-", dir=self.work)) / "collected"
                selected = fs.selection(handoff, kind="family-cache", coverage_sha=named)
                outcome = self.collect(handoff, output=output, selected=selected)
                self.assertEqual((outcome.status, outcome.carried_from), ("available", self.c0.commit))
                self.assertEqual((output / SELECTED_NAME).read_bytes(), canonical_json(selected))
        cases = {
            "a commit the envelope coverage does not precede": (self.s.commit, "does not precede"),
            "a commit that is not an ancestor of the coverage": (self.s.commit, "not named by an ancestor"),
            "an absent commit": ("9" * 40, "not present as an inert object"),
        }
        current = self.handoff()
        for label, (named, fragment) in cases.items():
            with self.subTest(label=label):
                # ``s`` forks from C0: C1's envelope never precedes it, and C0's does but C1 is no descendant.
                source = current if fragment == "does not precede" else handoff
                selected = fs.selection(source, kind="family-cache", coverage_sha=named)
                self.assert_rejected(lambda: self.collect(source, selected=selected), fragment)
        self.assertEqual(self.host.calls, [("family_validate", False)] * 2, "a refused selection never reaches the hook")

    def test_a_family_handoff_selection_is_its_envelope_producers_upload(self) -> None:
        handoff = self.handoff()
        valid = fs.selection(handoff)
        cases = {
            "another producer run": ({**valid, "run_id": valid["run_id"] + 1}, "is not the upload"),
            "another attempt": ({**valid, "run_attempt": 2, "name": grammar.family_handoff_name(fs.FAMILY, fs.KEY, 2)},
                                "is not the upload"),
            "another key": ({**valid, "name": grammar.family_handoff_name(fs.FAMILY, "mc26.3", 1)},
                            "not a generation of"),
            "an ordinary handoff": ({**valid, "kind": "handoff", "name": grammar.handoff_name(fs.KEY, 1)},
                                    "not a generation of"),
        }
        for label, (selected, fragment) in cases.items():
            with self.subTest(label=label):
                error = self.assert_rejected(lambda: self.collect(handoff, selected=selected), fragment)
                self.assertEqual(error.reason, "family-selection")
        self.assertEqual(self.host.calls, [])

    def test_a_malformed_selection_is_refused_before_the_envelope(self) -> None:
        handoff = self.handoff()
        valid = fs.selection(handoff)
        cases = {
            "not canonical": json.dumps(valid, indent=2).encode("utf-8"),
            "an extra member": canonical_json({**valid, "note": "x"}),
            "not an object": canonical_json([valid]),
            "not JSON": b"{",
            "a boolean id": canonical_json({**valid, "artifact_id": True}),
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                record = Path(tempfile.mkdtemp(prefix="selected-", dir=self.work)) / "selected.json"
                record.write_bytes(data)
                error = self.assert_rejected(lambda: collect_family(
                    self.invocation, family=fs.FAMILY, key=fs.KEY, input_dir=handoff,
                    expected_coverage_sha=self.c1.commit, output=self.work / "collected", selected_json=record))
                self.assertEqual(error.reason, "family-selection")
        linked = Path(tempfile.mkdtemp(prefix="selected-", dir=self.work)) / "selected.json"
        linked.symlink_to(self.selected_json(valid))
        self.assert_rejected(lambda: collect_family(
            self.invocation, family=fs.FAMILY, key=fs.KEY, input_dir=handoff, expected_coverage_sha=self.c1.commit,
            output=self.work / "collected", selected_json=linked), "symlink", "family-selection")
        self.assertEqual(self.host.calls, [])

    def test_the_kit_is_the_producer_workflows_pin_at_the_producer_commit(self) -> None:
        workflow = fs.PRODUCER_WORKFLOW
        forged = self.handoff()
        fs.rewrite_envelope(forged, lambda envelope: envelope["kit"].update(sha="2" * 40))
        self.assert_rejected(lambda: self.collect(forged), "is not the pin of", "kit-binding")
        cases = {
            "another kit": ({workflow: support.pin_workflow(workflow, sha="3" * 40)}, "is not the pin of"),
            "another version": ({workflow: support.pin_workflow(workflow, version="9.9.9")}, "is not the pin of"),
            "no pin": ({workflow: b"name: Family producer\njobs: {}\n"}, "cannot read the kit pin"),
        }
        for position, (label, (changes, fragment)) in enumerate(cases.items()):
            with self.subTest(label=label):
                head = support.commit_on_branch(self.c1, f"kit-{position}", changes)
                handoff = self.handoff(subject=head)
                self.assert_rejected(lambda: self.collect(handoff, expected=head.commit), fragment, "kit-binding")
        self.assertEqual(self.host.calls, [], "a generation of another kit never reaches the hook")


class ReadBlobTest(unittest.TestCase):
    """``family._git.read_blob``: a bounded regular file of a present commit, never anything else."""

    def test_only_a_bounded_regular_file_of_a_present_commit_is_read(self) -> None:
        from mod_base.family import _git

        root = Path(tempfile.mkdtemp(prefix="mb-read-blob-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "repo"
        (repo / "sub").mkdir(parents=True)
        (repo / "a.txt").write_bytes(b"hello")
        (repo / "run.sh").write_bytes(b"#!/bin/sh\n")
        (repo / "run.sh").chmod(0o755)
        (repo / "sub" / "b.txt").write_bytes(b"b")
        (repo / "link").symlink_to("a.txt")
        support.git(repo, "init", "-q", "--initial-branch=main")
        support.git(repo, "add", "-A")
        support.git(repo, "commit", "-q", "-m", "blobs")
        commit = support.git(repo, "rev-parse", "HEAD")
        self.assertEqual(_git.read_blob(repo, commit, "a.txt", 5), b"hello")
        self.assertEqual(_git.read_blob(repo, commit, "run.sh", 64), b"#!/bin/sh\n")
        self.assertEqual(_git.read_blob(repo, commit, "sub/b.txt", 1), b"b")
        cases = {
            "larger than the bound": (commit, "a.txt", 4, "larger than"),
            "a directory": (commit, "sub", 64, "not a regular file"),
            "a symlink": (commit, "link", 64, "not a regular file"),
            "an absent path": (commit, "absent.txt", 64, "not a file"),
            "an absent commit": ("9" * 40, "a.txt", 64, "not present as an inert object"),
            "a traversal": (commit, "../a.txt", 64, "not a canonical repository path"),
            "a zero bound": (commit, "a.txt", 0, "positive integer"),
        }
        for label, (at, path, bound, fragment) in cases.items():
            with self.subTest(label=label), self.assertRaises(MbError) as caught:
                _git.read_blob(repo, at, path, bound)
            self.assertIn(fragment, str(caught.exception))
            self.assertEqual(caught.exception.reason, "git")


class CollectCommandTest(CollectTest):
    def run_cli(self, input_dir: Path, *, expected: str | None = None) -> tuple[int, str, str]:
        outputs = Path(tempfile.mkdtemp(prefix="outputs-", dir=self.work)) / "github-output"
        outputs.touch()
        environ = {**fs.family_environment(self.c1), "GITHUB_OUTPUT": str(outputs)}
        stderr = io.StringIO()
        with mock.patch.object(cli, "environ", return_value=environ), contextlib.redirect_stderr(stderr):
            code = cli.main(["family", "collect", "--repo", str(self.c1.root), "--family", fs.FAMILY, "--key", fs.KEY,
                             "--input", str(input_dir), "--expected-coverage-sha", expected or self.c1.commit,
                             "--selected-json", str(self.selected_json(fs.selection(input_dir))),
                             "--output", str(self.work / "collected")])
        return code, stderr.getvalue(), outputs.read_text(encoding="utf-8")

    def test_an_available_family_exits_zero_with_its_status(self) -> None:
        handoff = self.handoff()
        code, stderr, outputs = self.run_cli(handoff)
        self.assertEqual((code, stderr, outputs), (0, "", "status=available\navailable=true\n"))
        self.assertTrue((self.work / "collected" / PROJECTION_NAME).is_file())

    def test_superseded_and_unavailable_exit_three_without_an_upload(self) -> None:
        for status in ("superseded", "unavailable"):
            with self.subTest(status=status):
                handoff = self.handoff(adjust=lambda manifest: manifest.update(outcome=status))
                code, stderr, outputs = self.run_cli(handoff)
                self.assertEqual(code, 3)
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertIn(f"fixture {status}", stderr)
                self.assertEqual(outputs, f"status={status}\navailable=false\n")
                self.assertFalse((self.work / "collected").exists())

    def test_a_rejected_projection_exits_two_without_an_upload(self) -> None:
        handoff = self.handoff(projection_adjust=lambda projection: projection["lanes"][0]["pairs"][0]["verdict"]
                               .update(semantic_valid=False))
        code, stderr, outputs = self.run_cli(handoff)
        self.assertEqual((code, outputs), (2, ""))
        self.assertIn("not publishable", stderr)
        self.assertFalse((self.work / "collected").exists())


if __name__ == "__main__":
    unittest.main()
