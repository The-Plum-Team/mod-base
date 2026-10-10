"""Every released bootstrap still stages this kit and bumps a mod to it.

A protected controller runs the bootstrap of its base branch, which can be any released one, and it
stages the kit a candidate pins; a mod bumps with the bootstrap it holds. So the three distinct
bootstraps of v0.9.0 to v1.0.3 run here for real, loaded from the archived ``template/`` trees in
``tests/fixtures/released_templates`` (each is ``git archive <tag> template``; the test proves it
by the tag's git tree and blob ids), against a git repository holding this kit's tree.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

from mod_base import pin
from mod_base.build_ci.activation import CALLERS
from mod_base.template import tool
from tests.test_bootstrap_bump_planning import BOOT_BYTES, FIXTURES, KIT_ROOT, KitsCase, current_kit
from tests.test_pin import git, mod_repo
from tests.test_template_callers import enter, real_mod

#: Archived tag -> (git tree id of its ``template/``, git blob id of its bootstrap, the releases
#: that carry that bootstrap). ``git rev-parse <tag>:template`` reproduces each id.
RELEASED = {
    "v0.9.1": ("e1706067de08992d5adcfbff7eb50c554d056f22", "c31da8497631972dd130c81281cd83249c52db1c",
               ("v0.9.0", "v0.9.1")),
    "v1.0.1": ("76360e45c54c5148dbbdeb3dd5910bb9e9d658bd", "3eb7bc57dc6d14ebbbf2e4c0f7866ff7d5b75090",
               ("v0.9.2", "v0.9.3", "v1.0.0", "v1.0.1")),
    "v1.0.3": ("e3c42620b5cec114913585f43bc22a91ee1ee4f3", "68d7a120f2ea29e49212c0b8f47ad85dc16c9dee",
               ("v1.0.2", "v1.0.3")),
}
BOOTSTRAP = "template/managed/scripts/ci/mod_base_kit.py"


def extract(tag: str, destination: Path) -> Path:
    with zipfile.ZipFile(FIXTURES / "released_templates" / f"{tag}.zip") as archive:
        archive.extractall(destination)
    return destination


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasedFixtureTest(unittest.TestCase):
    def test_each_archive_is_the_template_tree_of_its_tag(self) -> None:
        self.assertEqual(sorted(path.name for path in (FIXTURES / "released_templates").iterdir()),
                         [f"{tag}.zip" for tag in RELEASED])
        with tempfile.TemporaryDirectory(prefix="mb-released-") as directory:
            home = Path(directory) / "home"
            home.mkdir()
            for tag, (tree_id, blob_id, _releases) in RELEASED.items():
                work = extract(tag, Path(directory) / tag)
                git(work, "init", "-q", home=home)
                git(work, "add", "-A", home=home)
                with self.subTest(tag=tag):
                    self.assertEqual(git(work, "rev-parse", f"{git(work, 'write-tree', home=home)}:template", home=home),
                                     tree_id)
                    self.assertEqual(git(work, "hash-object", BOOTSTRAP, home=home), blob_id)
        self.assertEqual(sum(len(releases) for _tree, _blob, releases in RELEASED.values()), 8)


class ReleasedBootstrapTest(KitsCase):
    RELEASES = {"v1.1.1": current_kit}

    def bootstraps(self) -> dict[str, Any]:
        loaded = {}
        for tag in RELEASED:
            module = load(extract(tag, self.root / f"released-{tag}") / BOOTSTRAP, f"released_bootstrap_{tag[1:].replace('.', '_')}")
            self.patch(module)
            loaded[tag] = module
        return loaded

    def test_every_released_bootstrap_stages_this_kit_with_its_caller_templates(self) -> None:
        controller = mod_repo(self.root / "controller", "c" * 40, "v1.0.3")
        candidate = real_mod(self.root / "candidate", self.sha("v1.1.1"), "v1.1.1")
        for tag, bootstrap in self.bootstraps().items():
            with self.subTest(bootstrap=tag):
                output = self.root / f"staged-{tag}"
                environ = {"MOD_BASE_CACHE_DIR": str(self.root / f"cache-{tag}"), "CI": "true"}
                self.assertEqual(bootstrap.stage(controller, candidate, output, environ, get_json=self.getter("v1.1.1")),
                                 output)
                staged = sorted(path.name for path in output.iterdir())
                self.assertEqual("actions" in staged, tag != "v0.9.1", "a bootstrap older than v0.9.2 stages none")
                for path in CALLERS:
                    self.assertEqual((output / "template/managed" / path).read_bytes(),
                                     (KIT_ROOT / "template/managed" / path).read_bytes())
                pin.verify_staged_files(output)
                stamp = pin.read_stamp(output)
                self.assertEqual((stamp["sha"], stamp["tree_digest"]), (self.sha("v1.1.1"), pin.kit_tree_digest(KIT_ROOT)))
                overlay = candidate / pin.OVERLAY_PATH
                os.makedirs(overlay.parent, exist_ok=True)
                os.rename(output, overlay)
                self.assertEqual(pin.resolve(candidate, {"CI": "true"})[2], "overlay")
                self.assertEqual(tool.load_manifest(overlay)["kind"], "mod-base.template-manifest")
                os.rename(overlay, output)

    def test_every_released_bootstrap_reads_the_pin_of_a_mod_that_holds_the_callers(self) -> None:
        """A controller parses the candidate with its own bootstrap, whatever its release: the
        rendered callers, the guard's pin literal and the local call of the guard are all legal to
        the released parsers, and they find the same pin and references as this kit."""

        repo = real_mod(self.root / "active", self.sha("v1.1.1"), "v1.1.1")
        enter(repo, "shadow")
        tool.sync(repo, kit_root=KIT_ROOT, write=True)
        expected = pin.parse_pin(repo)
        self.assertEqual(len(expected.references), 3 + 5 + 1, "the Pages caller, the four callers and the mod's own")
        for tag, bootstrap in self.bootstraps().items():
            with self.subTest(bootstrap=tag):
                found = bootstrap.parse_pin(repo)
                self.assertEqual((found.sha, found.version, found.references),
                                 (expected.sha, expected.version, expected.references))

    def test_every_released_bootstrap_bumps_a_mod_to_this_kit_without_a_caller(self) -> None:
        for tag, bootstrap in self.bootstraps().items():
            with self.subTest(bootstrap=tag):
                repo = real_mod(self.root / f"mod-{tag}", "c" * 40, tag)
                (repo / "scripts/ci/mod_base_kit.py").write_bytes(
                    (self.root / f"released-{tag}" / BOOTSTRAP).read_bytes())
                environ = {"MOD_BASE_CACHE_DIR": str(self.root / f"bump-cache-{tag}")}
                result = bootstrap.bump(repo, "v1.1.1", environ, get_json=self.getter("v1.1.1"))
                self.assertEqual((result.sha, result.version), (self.sha("v1.1.1"), "v1.1.1"))
                self.assertEqual((repo / "scripts/ci/mod_base_kit.py").read_bytes(), BOOT_BYTES)
                self.assertFalse(any((repo / path).exists() for path in CALLERS))
                self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])


if __name__ == "__main__":
    unittest.main()
