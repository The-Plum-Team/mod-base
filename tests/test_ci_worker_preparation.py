"""Candidate staging: what binds the request to the tested tree, checked without root.

The tree name is recomputed here against Git itself, the reserved overlay slot and the closed
``stage-candidate`` request against authored data. The composed staging needs root and a really
allocated candidate and runs, with no seam, in ``tests/ci_linux_worker.py``.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import worker_preparation as prep
from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.source import GitSourceEntry, parse_source_inventory
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from mod_base.pin import Pin
from tests.helpers import ci_root_request
from tests.test_ci_git_fixture import git_fixture
from tests.test_ci_gradle_cache import CANDIDATE
from tests.test_ci_host import BOUNDARY
from tests.test_ci_worker_git import run_git


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


class TreeIdentityTests(unittest.TestCase):
    #: Names whose order differs between Git's rule (a directory sorts as ``name/``) and a plain
    #: sort of the names: ``lib`` is a directory and belongs after ``lib-old``, ``lib.d`` and ``lib.txt``.
    FILES = {
        ".hidden/config": ("100644", b"hidden\n"), "UPPER": ("100644", b"upper\n"), "_under": ("100644", b""),
        "empty": ("100644", b""), "lib/x": ("100644", b"x\n"), "lib/y/z": ("100644", b"z\n"),
        "lib-old": ("100644", b"old\n"), "lib.d/y": ("100644", b"y\n"), "lib.txt": ("100644", b"text\n"),
        "lib0": ("100644", b"zero\n"), "link": ("120000", b"lib/x"), "nested/deep/er/file": ("100644", b"deep\n"),
        "run.sh": ("100755", b"#!/bin/sh\n"),
    }

    def inventory(self):
        return tuple(GitSourceEntry(name, mode, len(data), blob(data)) for name, (mode, data) in sorted(self.FILES.items()))

    def test_the_authored_one_file_tree_has_its_known_name(self):
        _, tree, _ = git_fixture()
        data = b"known tracked bytes\n"
        self.assertEqual(prep._tree_id((GitSourceEntry("file", "100644", len(data), blob(data)),)), tree)

    def test_the_sample_request_names_the_tree_of_its_own_inventory(self):
        arguments = ci_root_request("stage-candidate")["arguments"]
        inventory = tuple(GitSourceEntry(**entry) for entry in arguments["inventory"])
        self.assertEqual(prep._tree_id(inventory), arguments["tested_tree"])

    @unittest.skipUnless(sys.platform == "linux", "the fixture needs POSIX modes and links")
    def test_git_gives_nested_executable_and_linked_entries_the_same_tree_name(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_git("init", "-q", ".", cwd=root)
            for name, (mode, data) in self.FILES.items():
                leaf = root / name
                leaf.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    os.symlink(data, os.fsencode(leaf))
                else:
                    leaf.write_bytes(data)
                    leaf.chmod(0o755 if mode == "100755" else 0o644)
            run_git("add", "-A", cwd=root)
            run_git("commit", "-q", "-m", "tree", cwd=root)
            tree = run_git("rev-parse", "HEAD^{tree}", cwd=root).strip()
            listed = parse_source_inventory(run_git("ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD", cwd=root).encode())
        self.assertEqual(listed, self.inventory())
        self.assertEqual(prep._tree_id(listed), tree)

    def test_any_other_path_mode_or_blob_is_another_tree(self):
        inventory = self.inventory()
        expected = prep._tree_id(inventory)
        position = [entry.path for entry in inventory].index("lib.txt")
        entry = inventory[position]
        changed = {
            "blob": GitSourceEntry(entry.path, entry.mode, entry.size, blob(b"other\n")),
            "mode": GitSourceEntry(entry.path, "100755", entry.size, entry.git_blob),
            "name": GitSourceEntry("lib.txu", entry.mode, entry.size, entry.git_blob),
        }
        seen = {expected}
        for name, replacement in changed.items():
            with self.subTest(name=name):
                other = prep._tree_id((*inventory[:position], replacement, *inventory[position + 1:]))
                self.assertNotIn(other, seen)
                seen.add(other)
        self.assertNotIn(prep._tree_id((*inventory[:position], *inventory[position + 1:])), seen)
        # The size is not part of a tree: the blob name already binds every byte.
        self.assertEqual(prep._tree_id((*inventory[:position], GitSourceEntry(entry.path, entry.mode, 0, entry.git_blob),
                                        *inventory[position + 1:])), expected)

    def test_only_a_complete_valid_inventory_has_a_tree_name(self):
        entry = GitSourceEntry("file", "100644", 0, blob(b""))
        for inventory in ((), [entry], (entry, entry), (GitSourceEntry("zz", "100644", 0, blob(b"")), entry),
                          (GitSourceEntry(".git/config", "100644", 0, blob(b"")),),
                          (entry, GitSourceEntry("file/inside", "100644", 0, blob(b""))),
                          (GitSourceEntry("file", "040000", 0, blob(b"")),),
                          (GitSourceEntry("file", "160000", 0, "a" * 40),)):
            with self.subTest(inventory=inventory), self.assertRaises(MbError):
                prep._tree_id(inventory)


class OverlaySlotTests(unittest.TestCase):
    def collides(self, path):
        return prep._collides((GitSourceEntry(path, "100644", 0, blob(b"")),))

    def test_tracked_paths_beside_the_overlay_are_free(self):
        for path in ("out/notes.txt", "out/mod-base-kit-notes/x", "out/reports/summary.txt", "outer/x", "output.txt",
                     "src/out/mod-base-kit/x", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(self.collides(path))

    def test_out_itself_the_overlay_slot_and_their_case_aliases_are_taken(self):
        for path in ("out", "OUT", "out/mod-base-kit", "out/mod-base-kit/src/mod_base/__init__.py",
                     "out/Mod-Base-Kit/x", "out/MOD-BASE-KIT", "Out/notes.txt", "OUT/mod-base-kit/x"):
            with self.subTest(path=path):
                self.assertTrue(self.collides(path))


class StagingRequestTests(unittest.TestCase):
    def changed(self, mutate):
        document = ci_root_request("stage-candidate")
        mutate(document["arguments"])
        return document

    def test_the_sample_request_and_its_optional_shapes_validate(self):
        document = ci_root_request("stage-candidate")
        self.assertEqual(load_document(canonical_json(document), kind="mod-base.ci.root-request"), document)
        self.assertEqual(set(document["arguments"]),
                         {"candidate", "repository", "tested_sha", "tested_tree", "inventory", "source", "overlay",
                          "gradle_seed"})
        validate_root_request(self.changed(lambda a: a.update(gradle_seed=None)))
        validate_root_request(self.changed(lambda a: a["inventory"].extend(
            [{"path": "link", "mode": "120000", "size": 4, "git_blob": "c" * 40},
             {"path": "run.sh", "mode": "100755", "size": 0, "git_blob": "d" * 40}])))

    def test_a_request_names_no_program_destination_or_unknown_field(self):
        for mutate in (lambda a: a.update(program="/bin/sh"), lambda a: a.update(destination="/tmp/elsewhere"),
                       lambda a: a.update(generated_roots=["build"]), lambda a: a.pop("gradle_seed"),
                       lambda a: a.pop("inventory"), lambda a: a["overlay"].update(stamp={}),
                       lambda a: a["overlay"].pop("tree_digest"), lambda a: a["candidate"].update(home="/tmp/x"),
                       lambda a: a["inventory"][0].update(sha256="e" * 64)):
            with self.subTest(changed=sorted(self.changed(mutate)["arguments"])), self.assertRaises(MbError):
                validate_root_request(self.changed(mutate))

    def test_every_directory_root_reads_is_canonical_and_below_the_fenced_runner_home(self):
        paths = ("/tmp/candidate", "/home/runner", "/home/runner/", "/home/runnerx/candidate", "home/runner/candidate",
                 "/home/runner/../etc", "/home/runner/./candidate", "/home/runner//candidate",
                 "/home/runner/work/candidate/", "/home/runner/a\nb", "/home/runner/a\\b", "/home/runner/c:d",
                 "/home/runner/" + "x" * limits.MAX_CI_TOOL_PATH_BYTES, "", None, 7, ["/home/runner/candidate"])
        for path in paths:
            for field in ("source", "gradle_seed", "overlay"):
                if field == "gradle_seed" and path is None:
                    continue  # An absent seed is the one optional directory.

                def mutate(arguments, field=field, path=path):
                    if field == "overlay":
                        arguments["overlay"]["path"] = path
                    else:
                        arguments[field] = path

                with self.subTest(field=field, path=str(path)[:60]), self.assertRaises(MbError):
                    validate_root_request(self.changed(mutate))

    def test_identities_pin_and_accounts_keep_their_grammars(self):
        boundary = ci_root_request("stage-candidate")["boundary"]
        for mutate in (lambda a: a.update(repository="owner/project/extra"), lambda a: a.update(repository="owner"),
                       lambda a: a.update(tested_sha="A" * 40), lambda a: a.update(tested_sha="a" * 39),
                       lambda a: a.update(tested_tree="sha256:" + "a" * 64), lambda a: a.update(tested_tree=None),
                       lambda a: a["overlay"].update(sha="a" * 64), lambda a: a["overlay"].update(version="v1.0.3"),
                       lambda a: a["overlay"].update(version="1.0"), lambda a: a["overlay"].update(tree_digest="b" * 64),
                       lambda a: a["candidate"].update(uid=0), lambda a: a["candidate"].update(gid=True),
                       lambda a: a["candidate"].update(uid=limits.MIN_CI_WORKER_UID - 1),
                       lambda a: a["candidate"].update(uid=boundary["uid"]),
                       lambda a: a["candidate"].update(gid=boundary["gid"])):
            with self.subTest(arguments=str(self.changed(mutate)["arguments"])[:90]), self.assertRaises(MbError):
                validate_root_request(self.changed(mutate))

    def test_the_inventory_is_a_complete_valid_one_of_the_existing_kind(self):
        row = {"path": "zz", "mode": "100644", "size": 0, "git_blob": "c" * 40}
        for mutate in (lambda a: a.update(inventory=[]), lambda a: a.update(inventory={}),
                       lambda a: a["inventory"].insert(0, dict(row)),
                       lambda a: a["inventory"].append(dict(a["inventory"][0])),
                       lambda a: a["inventory"].insert(0, dict(row, path="File")),
                       lambda a: a["inventory"].append(dict(row, path="file/inside")),
                       lambda a: a["inventory"].append(dict(row, path=".git/config")),
                       lambda a: a["inventory"].append(dict(row, path="../escape")),
                       lambda a: a["inventory"].append(dict(row, mode="040000")),
                       lambda a: a["inventory"].append(dict(row, mode="160000")),
                       lambda a: a["inventory"].append(dict(row, mode="120000")),
                       lambda a: a["inventory"].append(dict(row, mode="120000", size=limits.MAX_CI_SOURCE_LINK_BYTES + 1)),
                       lambda a: a["inventory"].append(dict(row, size=-1)),
                       lambda a: a["inventory"].append(dict(row, size=True)),
                       lambda a: a["inventory"].append(dict(row, size=limits.MAX_CI_SOURCE_FILE_BYTES + 1)),
                       lambda a: a["inventory"].append(dict(row, git_blob="C" * 40))):
            with self.subTest(inventory=str(self.changed(mutate)["arguments"]["inventory"])[-110:]), \
                    self.assertRaises(MbError):
                validate_root_request(self.changed(mutate))

    def test_existing_source_and_request_caps_bound_the_inventory(self):
        document = self.changed(lambda a: a["inventory"].append(
            {"path": "zz", "mode": "100644", "size": 5, "git_blob": "c" * 40}))
        validate_root_request(document)
        for bound in ("MAX_CI_SOURCE_FILES", "MAX_CI_SOURCE_TREE_BYTES", "MAX_CI_SOURCE_ENTRIES",
                      "MAX_CI_ROOT_REQUEST_BYTES"):
            with self.subTest(bound=bound), patch.object(limits, bound, 1), self.assertRaises(MbError):
                validate_root_request(document)


@unittest.skipUnless(sys.platform == "linux", "the staging entries are Linux-only")
class RootOnlyTests(unittest.TestCase):
    def test_preparation_refuses_the_unprivileged_runner_before_it_looks_at_anything(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        arguments = ci_root_request("stage-candidate")["arguments"]
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            prep.prepare_privileged_worker_checkout(
                Path(arguments["source"]), None, Path(arguments["overlay"]["path"]), boundary=BOUNDARY,
                account=CANDIDATE, repository=arguments["repository"], tested_commit=arguments["tested_sha"],
                tested_tree=arguments["tested_tree"],
                inventory=tuple(GitSourceEntry(**entry) for entry in arguments["inventory"]),
                pin=Pin(arguments["overlay"]["sha"], "v" + arguments["overlay"]["version"], ()),
                expected_digest=arguments["overlay"]["tree_digest"])


if __name__ == "__main__":
    unittest.main()
