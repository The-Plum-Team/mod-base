"""``mod-base.family.envelope`` creation, validation and the ``family envelope`` command (MB4)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.config import parse_config
from mod_base.errors import MbError
from mod_base.family.envelope import (
    ENVELOPE_NAME,
    MAX_NATIVE_PATH_CHARS,
    MAX_NATIVE_PATH_DEPTH,
    create_envelope,
    validate_envelope_dir,
)
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.runtime import Invocation
from tests import test_family_support as fs
from tests.fixtures.mods import support


def with_family(invocation: Invocation, **changes: Any) -> Invocation:
    """``invocation`` whose configured family has ``changes`` applied."""

    data = json.loads(canonical_json(invocation.config.data))
    data["families"][0].update(changes)
    return dataclasses.replace(invocation, config=parse_config(canonical_json(data)))


def listing(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def deepest_native_path() -> str:
    """A native path of exactly :data:`MAX_NATIVE_PATH_DEPTH` components."""

    return "/".join(["d"] * (MAX_NATIVE_PATH_DEPTH - 1) + ["deep.json"])


def longest_native_path() -> str:
    """A native path of exactly :data:`MAX_NATIVE_PATH_CHARS` characters."""

    head = "a" * 100 + "/" + "b" * 100 + "/"
    return head + "c" * (MAX_NATIVE_PATH_CHARS - len(head))


class EnvelopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="mb-family-envelope-")).resolve()
        cls.mod = fs.family_mod(cls.root / "mod")
        cls.invocation = fs.producer_invocation(cls.mod)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))

    def create(self, bundle: Path, *, invocation: Invocation | None = None, output: Path | None = None,
               subject: dict[str, str] | None = None, producer: dict[str, Any] | None = None,
               coverage_sha: str | None = None, family: str = fs.FAMILY, key: str = fs.KEY) -> dict[str, Any]:
        return create_envelope(invocation or self.invocation, family=family, key=key, bundle_dir=bundle,
                               coverage_sha=coverage_sha or self.mod.commit,
                               subject=subject or {"branch": self.mod.branch, "commit": self.mod.commit},
                               producer=producer or fs.producer_claim(self.mod),
                               output=output or self.work / "handoff")

    def handoff(self, **native: Any) -> Path:
        self.create(fs.native_bundle(self.work / "bundle", self.mod, **native))
        return self.work / "handoff"

    def assert_rejected(self, action: Any, fragment: str = "") -> MbError:
        with self.assertRaises(MbError) as caught:
            action()
        self.assertIn(fragment, str(caught.exception))
        return caught.exception


class CreateTest(EnvelopeTest):
    def test_copies_the_native_bundle_byte_for_byte_and_records_its_exact_inventory(self) -> None:
        extra = {"reports/lane/review.json": b'{"clean":true}\n', ".nojekyll": b"x"}
        bundle = fs.native_bundle(self.work / "bundle", self.mod, extra=extra)
        envelope = self.create(bundle)
        output = self.work / "handoff"
        native = listing(bundle)
        written = listing(output)
        self.assertEqual({name: data for name, data in written.items() if name != ENVELOPE_NAME}, native)
        self.assertEqual(written[ENVELOPE_NAME], canonical_json(envelope))
        self.assertEqual(envelope["files"], [{"path": name, "sha256": sha256_hex(data), "size": len(data)}
                                             for name, data in sorted(native.items())])
        self.assertEqual(envelope["subject"], self.mod.subject)
        self.assertEqual(envelope["producer"], fs.producer_claim(self.mod))
        self.assertEqual(envelope["kit"]["sha"], support.KIT_SHA)
        self.assertEqual((envelope["repository"], envelope["family"], envelope["key"], envelope["coverage_sha"]),
                         (self.mod.repository, fs.FAMILY, fs.KEY, self.mod.commit))
        self.assertEqual(envelope["native"], {"manifest_path": "manifest.json",
                                              "manifest_sha256": sha256_hex(native["manifest.json"]),
                                              "kind": fs.NATIVE_KIND, "schema_version": 1})
        self.assertNotIn("carried_from", envelope)
        self.assertEqual(validate_envelope_dir(self.invocation, output), envelope)
        self.assertEqual(validate_envelope_dir(self.invocation, output, family=fs.FAMILY, key=fs.KEY), envelope)

    def test_the_subject_is_the_checked_out_head_of_the_producer_run(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        other = "2" * 40
        self.assert_rejected(lambda: self.create(bundle, subject={"branch": "master", "commit": other}),
                             "is not the subject commit")
        self.assert_rejected(lambda: self.create(bundle, producer=fs.producer_claim(self.mod, sha=other)),
                             "not the producer run's own checkout")
        self.assert_rejected(lambda: self.create(bundle, subject={"branch": "release", "commit": self.mod.commit}),
                             "not the producer run's own checkout")
        foreign = {**fs.producer_claim(self.mod), "workflow_path": support.SOURCE_WORKFLOW}
        self.assert_rejected(lambda: self.create(bundle, producer=foreign), "producer is not a")
        claim = fs.producer_claim(self.mod)
        for label, producer in {"not its own controller": {**claim, "controller_sha": other},
                                "unknown claim field": {**claim, "event": "push"}}.items():
            with self.subTest(label=label):
                self.assertRaises(MbError, self.create, bundle, producer=producer)
        self.assert_rejected(lambda: self.create(bundle, subject={"branch": "master", "commit": self.mod.commit,
                                                                  "tree": self.mod.tree}))
        self.assertFalse((self.work / "handoff").exists())

    def test_the_coverage_is_exactly_the_producer_checkout(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        for coverage in ("1" * 40, self.mod.tree):
            with self.subTest(coverage=coverage):
                self.assert_rejected(lambda: self.create(bundle, coverage_sha=coverage), "is not the subject commit")
        self.assertFalse((self.work / "handoff").exists())

    def test_native_paths_fit_below_the_collected_source_directory(self) -> None:
        deepest, longest = deepest_native_path(), longest_native_path()
        self.assertEqual((len(deepest.split("/")), len(longest)), (MAX_NATIVE_PATH_DEPTH, MAX_NATIVE_PATH_CHARS))
        bundle = fs.native_bundle(self.work / "bundle", self.mod, extra={deepest: b"{}", longest: b"{}"})
        paths = [record["path"] for record in self.create(bundle)["files"]]
        self.assertIn(deepest, paths)
        self.assertIn(longest, paths)
        cases = {
            "one component too deep": ({"d/" + deepest: b"{}"}, "components"),
            "one character too long": ({longest + "c": b"{}"}, "characters"),
            "the envelope name folded": ({"Envelope.JSON": b"{}"}, "collide under case folding"),
            "a directory folding onto the envelope name": ({"ENVELOPE.json/x.json": b"{}"},
                                                          "collide under case folding"),
        }
        for label, (extra, fragment) in cases.items():
            with self.subTest(label=label):
                work = Path(tempfile.mkdtemp(prefix="path-", dir=self.work))
                damaged = fs.native_bundle(work / "bundle", self.mod, extra=extra)
                self.assert_rejected(lambda: self.create(damaged, output=work / "handoff"), fragment)
                self.assertFalse((work / "handoff").exists())

    def test_identifiers_must_be_configured_and_well_formed(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        self.assert_rejected(lambda: self.create(bundle, family="other-family"), "not configured")
        for label, action in {
            "key": lambda: self.create(bundle, key="Bad--Key"),
            "family grammar": lambda: self.create(bundle, family="Mod_Compat"),
            "coverage": lambda: self.create(bundle, coverage_sha="HEAD"),
        }.items():
            with self.subTest(label=label):
                self.assertRaises(MbError, action)
        self.assertFalse((self.work / "handoff").exists())

    def test_unsafe_or_incomplete_native_bundles_are_refused(self) -> None:
        def symlinked_file(root: Path) -> None:
            (root / "link.json").symlink_to(root / "manifest.json")

        def symlinked_directory(root: Path) -> None:
            (root / "linked").symlink_to(root / "images", target_is_directory=True)

        def hard_link(root: Path) -> None:
            os.link(root / "manifest.json", root / "hard.json")

        def empty_file(root: Path) -> None:
            (root / "empty.json").write_bytes(b"")

        def own_envelope(root: Path) -> None:
            (root / ENVELOPE_NAME).write_text("{}", encoding="utf-8")

        def no_manifest(root: Path) -> None:
            (root / "manifest.json").rename(root / "native.json")

        def fifo(root: Path) -> None:
            os.mkfifo(root / "pipe")

        def manifest(value: bytes) -> Any:
            return lambda root: (root / "manifest.json").write_bytes(value)

        cases = {
            "symlinked file": (symlinked_file, "symlink"),
            "symlinked directory": (symlinked_directory, "symlink"),
            "hard link": (hard_link, "hard-linked"),
            "empty file": (empty_file, "size"),
            "special file": (fifo, "special file"),
            "envelope name": (own_envelope, "must not hold envelope.json"),
            "no manifest": (no_manifest, "no root manifest.json"),
            "manifest is not JSON": (manifest(b"not json"), ""),
            "manifest duplicate key": (manifest(b'{"kind":"a","kind":"b","schema_version":1}'), ""),
            "manifest is an array": (manifest(b"[1]"), "string kind and an integer schema_version"),
            "manifest without kind": (manifest(b'{"schema_version":1}'), "string kind"),
            "boolean schema version": (manifest(b'{"kind":"a","schema_version":true}'), "integer schema_version"),
            "text schema version": (manifest(b'{"kind":"a","schema_version":"1"}'), "integer schema_version"),
            "zero schema version": (manifest(b'{"kind":"a","schema_version":0}'), "schema_version"),
            "unsafe native kind": (manifest(b'{"kind":"Quick Skin","schema_version":1}'), "native.kind"),
        }
        for label, (damage, fragment) in cases.items():
            with self.subTest(label=label):
                work = Path(tempfile.mkdtemp(prefix="unsafe-", dir=self.work))
                bundle = fs.native_bundle(work / "bundle", self.mod)
                damage(bundle)
                self.assert_rejected(lambda: self.create(bundle, output=work / "handoff"), fragment)
                self.assertEqual([path.name for path in work.iterdir()], ["bundle"])
        link = self.work / "bundle-link"
        link.symlink_to(fs.native_bundle(self.work / "real-bundle", self.mod), target_is_directory=True)
        self.assert_rejected(lambda: self.create(link), "must be a real directory")
        self.assert_rejected(lambda: self.create(self.work / "absent"), "cannot inspect")

    def test_the_family_byte_bound_is_enforced(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        size = sum(len(data) for data in listing(bundle).values())
        self.create(bundle, invocation=with_family(self.invocation, handoff_max_bytes=size))
        smaller = with_family(self.invocation, handoff_max_bytes=size - 1)
        self.assert_rejected(lambda: self.create(bundle, invocation=smaller, output=self.work / "too-large"), "bound")
        self.assertFalse((self.work / "too-large").exists())

    def test_the_output_is_new_and_outside_the_bundle(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        (self.work / "handoff").mkdir()
        self.assert_rejected(lambda: self.create(bundle), "existing output")
        self.assert_rejected(lambda: self.create(bundle, output=bundle / "handoff"), "outside the native bundle")
        self.assert_rejected(lambda: self.create(bundle, output=bundle), "outside the native bundle")
        self.assertFalse((bundle / "handoff").exists())


class ValidateTest(EnvelopeTest):
    def test_the_directory_must_hold_exactly_the_enveloped_files(self) -> None:
        output = self.handoff()
        pristine = listing(output)

        def reset() -> None:
            shutil.rmtree(output)
            fs.write_tree(output, pristine)

        def flip(path: Path) -> None:
            data = bytearray(path.read_bytes())
            data[len(data) // 2] ^= 0x01
            path.write_bytes(bytes(data))

        image = next(name for name in pristine if name.startswith("images/"))
        cases = {
            "extra file": lambda: (output / "stray.txt").write_text("x", encoding="utf-8"),
            "extra nested file": lambda: fs.write_tree(output, {"images/deep/x.webp": b"x"}),
            "missing file": lambda: (output / image).unlink(),
            "changed byte": lambda: flip(output / image),
            "changed manifest": lambda: flip(output / "manifest.json"),
            "symlink": lambda: (output / "link").symlink_to(output / "manifest.json"),
            "missing envelope": lambda: (output / ENVELOPE_NAME).unlink(),
        }
        for label, damage in cases.items():
            with self.subTest(label=label):
                damage()
                self.assertRaises(MbError, validate_envelope_dir, self.invocation, output)
                reset()
        validate_envelope_dir(self.invocation, output)

    def test_forged_or_foreign_envelopes_are_rejected(self) -> None:
        output = self.handoff()
        pristine = listing(output)
        manifest = json.loads(pristine["manifest.json"])

        def reset() -> None:
            shutil.rmtree(output)
            fs.write_tree(output, pristine)

        def non_canonical() -> None:
            envelope = json.loads(pristine[ENVELOPE_NAME])
            (output / ENVELOPE_NAME).write_text(json.dumps(envelope, indent=2), encoding="utf-8")

        def native_kind(envelope: dict[str, Any]) -> None:
            envelope["native"]["kind"] = "other-native"

        def native_manifest_rewritten(envelope: dict[str, Any]) -> None:
            data = json.dumps({**manifest, "kind": "other-native"}).encode("utf-8")
            (output / "manifest.json").write_bytes(data)
            record = next(item for item in envelope["files"] if item["path"] == "manifest.json")
            record.update(sha256=sha256_hex(data), size=len(data))
            envelope["native"]["manifest_sha256"] = sha256_hex(data)

        forgeries: dict[str, Any] = {
            "unknown family": lambda envelope: envelope.update(family="other-family"),
            "foreign repository": lambda envelope: envelope.update(repository="The-Plum-Team/other"),
            "foreign producer workflow": lambda envelope: envelope["producer"].update(
                workflow_path=support.SOURCE_WORKFLOW),
            "subject is not the producer": lambda envelope: envelope["subject"].update(commit="3" * 40),
            "coverage is not the subject": lambda envelope: envelope.update(coverage_sha="6" * 40),
            "coverage carried beyond the subject": lambda envelope: envelope.update(
                coverage_sha="6" * 40, carried_from=envelope["subject"]["commit"]),
            "native kind differs from the manifest": native_kind,
            "native manifest disagrees": native_manifest_rewritten,
            "carried_from equal to coverage": lambda envelope: envelope.update(carried_from=envelope["coverage_sha"]),
            "unknown member": lambda envelope: envelope.update(note="x"),
            "unsorted files": lambda envelope: envelope["files"].reverse(),
        }
        for label, change in forgeries.items():
            with self.subTest(label=label):
                fs.rewrite_envelope(output, change)
                self.assertRaises(MbError, validate_envelope_dir, self.invocation, output)
                reset()
        with self.subTest(label="non-canonical"):
            non_canonical()
            self.assert_rejected(lambda: validate_envelope_dir(self.invocation, output), "canonical")
            reset()
        for label, arguments in {"other family": {"family": "other-family"}, "other key": {"key": "mc26.3"},
                                 "malformed key": {"key": "Bad"}}.items():
            with self.subTest(label=label):
                self.assertRaises(MbError, validate_envelope_dir, self.invocation, output, **arguments)
        foreign = dataclasses.replace(self.invocation, environ={**self.invocation.environ,
                                                                "GITHUB_REPOSITORY": "The-Plum-Team/other"})
        self.assert_rejected(lambda: validate_envelope_dir(foreign, output), "another repository")
        validate_envelope_dir(self.invocation, output)

    def test_forged_native_paths_are_rejected_before_the_inventory_walk(self) -> None:
        output = self.handoff()

        def listed(path: str) -> Any:
            def change(envelope: dict[str, Any]) -> None:
                envelope["files"].append({"path": path, "sha256": "7" * 64, "size": 2})
                envelope["files"].sort(key=lambda record: record["path"])
            return change

        pristine = listing(output)
        for label, path, fragment in (("too deep", "d/" + deepest_native_path(), "components"),
                                      ("too long", longest_native_path() + "c", "characters"),
                                      ("folded", "Manifest.json", "collide under case folding")):
            with self.subTest(label=label):
                fs.rewrite_envelope(output, listed(path))
                with mock.patch("mod_base.family.envelope.file_records") as walk:
                    self.assert_rejected(lambda: validate_envelope_dir(self.invocation, output), fragment)
                walk.assert_not_called()
                shutil.rmtree(output)
                fs.write_tree(output, pristine)
        validate_envelope_dir(self.invocation, output)

    def test_carried_from_requires_the_family_to_allow_carry_forward(self) -> None:
        output = self.handoff()
        fs.rewrite_envelope(output, lambda envelope: envelope.update(carried_from="4" * 40))
        self.assertEqual(validate_envelope_dir(self.invocation, output)["carried_from"], "4" * 40)
        error = self.assert_rejected(lambda: validate_envelope_dir(with_family(self.invocation, carry_forward=False),
                                                                   output), "does not allow carry-forward")
        self.assertEqual(error.reason, "carry-forward")

    def test_the_configured_byte_bound_applies_at_validation(self) -> None:
        output = self.handoff()
        size = sum(len(data) for name, data in listing(output).items() if name != ENVELOPE_NAME)
        validate_envelope_dir(with_family(self.invocation, handoff_max_bytes=size), output)
        self.assertRaises(MbError, validate_envelope_dir, with_family(self.invocation, handoff_max_bytes=size - 1),
                          output)

    def test_the_root_must_be_a_real_directory(self) -> None:
        output = self.handoff()
        link = self.work / "link"
        link.symlink_to(output, target_is_directory=True)
        self.assert_rejected(lambda: validate_envelope_dir(self.invocation, link), "real directory")
        self.assertRaises(MbError, validate_envelope_dir, self.invocation, self.work / "absent")


class EnvelopeCommandTest(EnvelopeTest):
    def run_cli(self, *arguments: str, environ: dict[str, str] | None = None) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch.object(cli, "environ", return_value=environ or fs.producer_environment(self.mod)), \
                contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stderr.getvalue()

    def arguments(self, bundle: Path, output: Path, *, commit: str | None = None) -> list[str]:
        return ["family", "envelope", "--repo", str(self.mod.root), "--family", fs.FAMILY, "--key", fs.KEY,
                "--bundle", str(bundle), "--coverage-sha", self.mod.commit, "--subject-branch", self.mod.branch,
                "--subject-commit", commit or self.mod.commit, "--output", str(output)]

    def test_family_envelope_writes_a_valid_handoff(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        code, stderr = self.run_cli(*self.arguments(bundle, self.work / "handoff"))
        self.assertEqual((code, stderr), (0, ""))
        envelope = validate_envelope_dir(self.invocation, self.work / "handoff", family=fs.FAMILY, key=fs.KEY)
        self.assertEqual(envelope["producer"], fs.producer_claim(self.mod))

    def test_family_envelope_rejects_a_foreign_subject_with_one_line(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        code, stderr = self.run_cli(*self.arguments(bundle, self.work / "handoff", commit="5" * 40),
                                    environ=fs.producer_environment(self.mod, sha="5" * 40))
        self.assertEqual(code, 2)
        self.assertEqual(len(stderr.splitlines()), 1)
        self.assertIn("is not the subject commit", stderr)
        self.assertFalse((self.work / "handoff").exists())

    def test_family_envelope_rejects_a_coverage_other_than_the_subject(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        arguments = self.arguments(bundle, self.work / "handoff")
        arguments[arguments.index("--coverage-sha") + 1] = "8" * 40
        code, stderr = self.run_cli(*arguments)
        self.assertEqual(code, 2)
        self.assertIn("is not the subject commit", stderr)
        self.assertFalse((self.work / "handoff").exists())

    def test_family_envelope_flags_are_checked_before_any_work(self) -> None:
        bundle = fs.native_bundle(self.work / "bundle", self.mod)
        arguments = self.arguments(bundle, self.work / "handoff")
        arguments[arguments.index("--coverage-sha") + 1] = "--upload-pack=x"
        code, stderr = self.run_cli(*arguments)
        self.assertEqual(code, 2)
        self.assertFalse((self.work / "handoff").exists())


if __name__ == "__main__":
    unittest.main()
