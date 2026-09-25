"""R4 (``validate_projection``: schema, publishability and image re-inspection) and R5
(``verify_carry_forward``: ancestry over inert Git objects) of ``mod_base.family.paired`` (MB4)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.errors import MbError
from mod_base.family.paired import validate_projection, verify_carry_forward
from mod_base.imaging.metrics import SizePolicy, inspect_webp
from mod_base.imaging.png import pattern_png
from mod_base.imaging.webp import derive_webp
from mod_base.model.canonical import sha256_hex
from tests import test_family_support as fs
from tests.fixtures.mods import support


class ProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="mb-family-paired-")).resolve()
        cls.mod = fs.family_mod(cls.root / "mod")
        cls.invocation = fs.family_invocation(cls.mod)
        cls.document, cls.images = fs.projection(subject=cls.mod.subject, producer=fs.producer_claim(cls.mod),
                                                 coverage_sha=cls.mod.commit, seeds=((1, 2), (1, 3)))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))

    def write(self, document: Any, images: dict[str, bytes] | None = None, *, name: str = "paired.json") -> Path:
        directory = Path(tempfile.mkdtemp(prefix="projection-", dir=self.work))
        fs.write_tree(directory, self.images if images is None else images)
        (directory / name).write_text(json.dumps(document, indent=2), encoding="utf-8")
        return directory

    def validate(self, directory: Path, *, name: str = "paired.json", family: str = fs.FAMILY, key: str = fs.KEY,
                 coverage: str | None = None, images_root: Path | None = None) -> dict[str, Any]:
        return validate_projection(self.invocation, directory / name, images_root=images_root or directory,
                                   family=family, key=key, expected_coverage_sha=coverage or self.mod.commit)

    def mutated(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        document = copy.deepcopy(self.document)
        change(document)
        return document

    def assert_rejected(self, directory: Path, fragment: str = "", **arguments: Any) -> MbError:
        with self.assertRaises(MbError) as caught:
            self.validate(directory, **arguments)
        self.assertIn(fragment, str(caught.exception))
        return caught.exception

    def test_a_valid_projection_is_returned_after_image_reinspection(self) -> None:
        directory = self.write(self.document)
        self.assertEqual(self.validate(directory), self.document)
        self.assertEqual(len(self.images), 3, "the shared reference image is stored once")
        with mock.patch("mod_base.family.paired.inspect_webp", wraps=inspect_webp) as inspected:
            self.validate(directory)
        self.assertEqual(inspected.call_count, 3)

    def test_unclean_pairs_are_never_publishable(self) -> None:
        for field, value in (("defect", True), ("semantic_valid", False), ("runtime_passed", False)):
            with self.subTest(field=field):
                document = self.mutated(lambda item: item["lanes"][0]["pairs"][1]["verdict"].update({field: value}))
                self.assert_rejected(self.write(document), "not publishable")
        document = self.mutated(lambda item: item["lanes"][0]["pairs"][0]["verdict"].update(matches_reference=False))
        self.validate(self.write(document))

    def test_unknown_keys_are_rejected_at_every_level(self) -> None:
        def add(*path: str | int) -> Callable[[dict[str, Any]], None]:
            def change(document: dict[str, Any]) -> None:
                target: Any = document
                for part in path:
                    target = target[part]
                target["note"] = "x"
            return change

        places = {
            "top level": add(),
            "provenance": add("provenance"),
            "lane": add("lanes", 0),
            "variant": add("lanes", 0, "variant"),
            "pair": add("lanes", 0, "pairs", 0),
            "verdict": add("lanes", 0, "pairs", 0, "verdict"),
            "image": add("lanes", 0, "pairs", 0, "candidate", "image"),
            "pixel": add("lanes", 0, "pairs", 0, "candidate", "image", "pixel"),
            "source": add("lanes", 0, "pairs", 0, "reference", "source"),
            "not applicable": add("not_applicable", 0),
        }
        for label, change in places.items():
            with self.subTest(place=label):
                self.assert_rejected(self.write(self.mutated(change)), "note")

    def test_recorded_dimensions_must_be_the_thumbnail_of_the_source(self) -> None:
        def narrower(document: dict[str, Any]) -> None:
            image = document["lanes"][0]["pairs"][0]["candidate"]["image"]
            image["width"] -= 1
            image["pixel"]["width"] -= 1

        def other_source(document: dict[str, Any]) -> None:
            source = document["lanes"][0]["pairs"][0]["candidate"]["source"]
            source["width"] = source["pixel"]["width"] = 1280
            source["height"] = source["pixel"]["height"] = 1024

        for label, change in {"derivative": narrower, "source": other_source}.items():
            with self.subTest(label=label):
                self.assert_rejected(self.write(self.mutated(change)), "thumbnail")

    def test_an_image_of_other_dimensions_fails_reinspection(self) -> None:
        webp = derive_webp(pattern_png(180, 108, 5), box=fs.BOX, quality=80, method=6)
        digest = sha256_hex(webp)
        forged = {**inspect_webp(webp, SizePolicy.exact(150, 90)), "width": 160, "height": 90}
        self.assertEqual(inspect_webp(webp, SizePolicy.minimum(1, 1))["width"], 150)

        def swap(document: dict[str, Any]) -> None:
            image = document["lanes"][0]["pairs"][0]["candidate"]["image"]
            images.pop(image["path"])
            image.update(path=f"images/{digest}.webp", sha256=digest, size=len(webp), pixel=forged)
            images[image["path"]] = webp

        images = dict(self.images)
        self.assert_rejected(self.write(self.mutated(swap), images), "re-inspection")

    def test_recorded_pixel_metrics_must_equal_the_kits(self) -> None:
        changes = {
            "entropy": lambda pixel: pixel.update(luma_entropy=pixel["luma_entropy"] + 0.001),
            "colours": lambda pixel: pixel.update(meaningful_colors=pixel["meaningful_colors"] - 1),
            "pixel digest": lambda pixel: pixel.update(pixel_sha256="0" * 64),
            "integer for a float": lambda pixel: pixel.update(dark_fraction=int(pixel["dark_fraction"])),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                document = self.mutated(
                    lambda item: change(item["lanes"][0]["pairs"][0]["candidate"]["image"]["pixel"]))
                self.assert_rejected(self.write(document), "pixel metrics")

    def test_image_bytes_must_match_their_records(self) -> None:
        candidate = self.document["lanes"][0]["pairs"][0]["candidate"]["image"]["path"]
        other_webp = derive_webp(pattern_png(*fs.SOURCE_SIZE, 9), box=fs.BOX, quality=80, method=6)
        png = pattern_png(160, 90, 4)
        cases = {
            "other bytes under the name": {**self.images, candidate: other_webp},
            "missing image": {name: data for name, data in self.images.items() if name != candidate},
            "extra image": {**self.images, f"images/{'9' * 64}.webp": other_webp},
            "nested file": {**self.images, "images/deep/x.webp": other_webp},
        }
        for label, images in cases.items():
            with self.subTest(label=label):
                self.assertRaises(MbError, self.validate, self.write(self.document, images))

        def as_png(document: dict[str, Any]) -> None:
            image = document["lanes"][0]["pairs"][0]["candidate"]["image"]
            image.update(path=f"images/{sha256_hex(png)}.webp", sha256=sha256_hex(png), size=len(png))
            image["pixel"]["file_sha256"] = sha256_hex(png)

        document = self.mutated(as_png)
        images = {name: data for name, data in self.images.items() if name != candidate}
        images[f"images/{sha256_hex(png)}.webp"] = png
        self.assert_rejected(self.write(document, images), "re-inspection")

    def test_images_are_never_followed_through_symlinks(self) -> None:
        candidate = self.document["lanes"][0]["pairs"][0]["candidate"]["image"]["path"]
        directory = self.write(self.document)
        target = self.work / "elsewhere.webp"
        (directory / candidate).rename(target)
        (directory / candidate).symlink_to(target)
        self.assertRaises(MbError, self.validate, directory)
        linked = self.write(self.document, {})
        shutil.rmtree(linked / "images", ignore_errors=True)
        (linked / "images").symlink_to(self.write(self.document) / "images", target_is_directory=True)
        self.assertRaises(MbError, self.validate, linked)
        projection_link = self.write(self.document)
        (projection_link / "paired.json").rename(projection_link / "real.json")
        (projection_link / "paired.json").symlink_to(projection_link / "real.json")
        self.assertRaises(MbError, self.validate, projection_link)

    def test_a_shared_image_path_carries_one_record(self) -> None:
        document = self.mutated(lambda item: item["lanes"][0]["pairs"][1]["reference"]["source"].update(
            sha256="8" * 64))
        document["lanes"][0]["pairs"][1]["reference"]["source"]["pixel"]["file_sha256"] = "8" * 64
        self.validate(self.write(document))
        document = self.mutated(lambda item: item["lanes"][0]["pairs"][1]["reference"]["image"].update(size=1))
        self.assert_rejected(self.write(document), "different facts")

    def test_identity_policy_and_coverage_are_bound(self) -> None:
        directory = self.write(self.document)
        error = self.assert_rejected(directory, "covers", coverage="6" * 40)
        self.assertEqual(error.reason, "stale-coverage")
        self.assert_rejected(directory, "key", key="mc26.3")
        self.assert_rejected(directory, "not configured", family="other-family")
        for label, change in {
            "policy quality": lambda item: item["image_policy"].update(webp_quality=81),
            "policy box": lambda item: item["image_policy"].update(derivative_box=[1280, 720]),
            "status": lambda item: item.update(status="superseded"),
            "kind": lambda item: item.update(kind="mod-base.family.envelope"),
            "producer is not its own controller": lambda item: item["provenance"]["producer"].update(
                controller_sha="7" * 40),
            "duplicate link label": lambda item: item["provenance"]["links"].append(
                {"label": "Compatibility runtime", "run_id": 404}),
            "image outside images/": lambda item: item["lanes"][0]["pairs"][0]["candidate"]["image"].update(
                path="other/x.webp"),
        }.items():
            with self.subTest(label=label):
                self.assertRaises(MbError, self.validate, self.write(self.mutated(change)))

    def test_the_projection_file_is_strict_bounded_json(self) -> None:
        cases = {
            "not json": b"{",
            "duplicate key": b'{"kind":"a","kind":"b"}',
            "nan": b'{"kind":NaN}',
            "array": b"[]",
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                directory = self.write(self.document)
                (directory / "paired.json").write_bytes(data)
                self.assertRaises(MbError, self.validate, directory)
        directory = self.write(self.document)
        with mock.patch("mod_base.model.limits.MAX_PAIRED_BYTES", 64):
            self.assertRaises(MbError, self.validate, directory)

    def test_the_images_fit_the_family_projection_budget(self) -> None:
        # The images' share of a collected family (limits.MAX_FAMILY_PROJECTION_BYTES), not a handoff's.
        directory = self.write(self.document)
        total = sum(len(data) for data in self.images.values())
        with mock.patch("mod_base.model.limits.MAX_FAMILY_PROJECTION_BYTES", total):
            self.validate(directory)
        with mock.patch("mod_base.model.limits.MAX_FAMILY_PROJECTION_BYTES", total - 1):
            self.assert_rejected(directory, f"the projection images exceed {total - 1} bytes")

    def test_a_projection_without_lanes_needs_no_images(self) -> None:
        document = self.mutated(lambda item: item.update(lanes=[]))
        directory = self.write(document, {})
        self.assertEqual(self.validate(directory)["lanes"], [])
        fs.write_tree(directory, {"images/stray.webp": b"x"})
        self.assertRaises(MbError, self.validate, directory)


def git(root: Path, *arguments: str) -> str:
    return support.git(root, *arguments)


class CarryForwardTest(unittest.TestCase):
    """R5 against a local repository: A <- B on master, S forked from A on ``side``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="mb-family-r5-")).resolve()
        cls.repo = cls.root / "repo"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "--initial-branch=master")
        cls.a = cls.commit("a")
        cls.b = cls.commit("b")
        git(cls.repo, "checkout", "-q", "-b", "side", cls.a)
        cls.s = cls.commit("s")
        git(cls.repo, "checkout", "-q", "master")
        git(cls.repo, "tag", "-a", "-m", "annotated", "v1", cls.a)
        cls.tag = git(cls.repo, "rev-parse", "v1")
        cls.blob = git(cls.repo, "rev-parse", f"{cls.a}:a")

    @classmethod
    def commit(cls, name: str) -> str:
        (cls.repo / name).write_text(name, encoding="utf-8")
        git(cls.repo, "add", name)
        git(cls.repo, "commit", "-q", "-m", name)
        return git(cls.repo, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def assert_refused(self, carried_from: str, coverage: str, reason: str, repo: Path | None = None) -> None:
        with self.assertRaises(MbError) as caught:
            verify_carry_forward(repo or self.repo, carried_from, coverage)
        self.assertEqual(caught.exception.reason, reason)

    def test_an_ancestor_carries_forward(self) -> None:
        verify_carry_forward(self.repo, self.a, self.b)
        verify_carry_forward(self.repo, self.a, self.s)

    def test_non_ancestors_never_carry_forward(self) -> None:
        self.assert_refused(self.b, self.s, "carry-forward")
        self.assert_refused(self.s, self.b, "carry-forward")
        self.assert_refused(self.b, self.a, "carry-forward")
        self.assert_refused(self.a, self.a, "carry-forward")

    def test_both_commits_must_be_present_commit_objects(self) -> None:
        missing = "1" * 40
        self.assert_refused(missing, self.b, "git")
        self.assert_refused(self.a, missing, "git")
        self.assert_refused(self.tag, self.b, "git")
        self.assert_refused(self.blob, self.b, "git")

    def test_malformed_identifiers_are_refused_before_git_runs(self) -> None:
        with mock.patch("mod_base.family._git.subprocess.run") as run:
            for value in ("HEAD", "--upload-pack=x", self.a.upper(), self.a[:12], ""):
                with self.subTest(value=value):
                    self.assertRaises(MbError, verify_carry_forward, self.repo, value, self.b)
                    self.assertRaises(MbError, verify_carry_forward, self.repo, self.a, value)
        run.assert_not_called()

    def test_replacement_refs_and_the_inherited_environment_are_ignored(self) -> None:
        clone = self.root / "replaced"
        shutil.copytree(self.repo, clone)
        git(clone, "replace", "--graft", self.b, self.s)
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", self.s, self.b],
                                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(clone),
                                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                                        check=False).returncode, 0, "the replacement makes S look like an ancestor")
        self.assert_refused(self.s, self.b, "carry-forward", repo=clone)
        other = self.root / "other-git-dir"
        other.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other), "GIT_OBJECT_DIRECTORY": str(other),
                                          "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other)}):
            verify_carry_forward(self.repo, self.a, self.b)

    def test_a_directory_that_is_not_a_repository_is_refused(self) -> None:
        inside = self.repo / "plain"
        inside.mkdir(exist_ok=True)
        self.assert_refused(self.a, self.b, "git", repo=inside)
        self.assert_refused(self.a, self.b, "git", repo=self.root / "absent")


if __name__ == "__main__":
    unittest.main()
