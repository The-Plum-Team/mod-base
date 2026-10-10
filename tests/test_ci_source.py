"""Protected source inventory parser and independently bound copy verification."""

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci.source import (GitSourceEntry, materialize_source_copy,
                                      authenticate_source_inventory, parse_source_inventory, verify_source_copy)
from mod_base.errors import MbError
from mod_base.io.tree import source_records
from mod_base.model import limits
from tests.test_ci_protocol import protected_subject, seeded_pr


def listing(path=b"src/main.py", *, mode=b"100644", size=b"3", blob=b"a" * 40):
    return mode + b" blob " + blob + b"       " + size + b"\t" + path + b"\0"


class SourceInventoryTests(unittest.TestCase):
    def test_unsorted_listing_empty_file_executable_and_link_are_preserved(self):
        data = (listing(b"z", mode=b"120000", size=b"2")
                + listing(b"b", mode=b"100755") + listing(b"a", size=b"0"))
        inventory = parse_source_inventory(data)
        self.assertEqual([(entry.path, entry.mode, entry.size) for entry in inventory],
                         [("a", "100644", 0), ("b", "100755", 3), ("z", "120000", 2)])

    def test_hostile_listing_rejected(self):
        cases = [b"", listing()[:-1], listing() + b"\0", listing() * 2,
                 listing(b"../x"), listing(b".git/config"), listing(b"x/.GIT/y"),
                 listing(b"x\tq"), listing(b"x\ny"), listing(b"\xff"),
                 listing(mode=b"160000"), listing(mode=b"100600"),
                 listing(blob=b"A" * 40), listing(size=b"01"), listing(size=b"-1"),
                 listing(size=b"9" * 5000), listing(mode=b"120000", size=b"0"),
                 listing(b"x") + listing(b"x/y"), listing(b"Foo/x") + listing(b"foo/y")]
        for data in cases:
            with self.subTest(data=data[:90]), self.assertRaises(MbError):
                parse_source_inventory(data)

    def test_caps_enforced_before_conversion_or_copy(self):
        for name, cap, data in [("MAX_CI_SOURCE_LIST_BYTES", 1, listing()),
                                ("MAX_CI_SOURCE_FILES", 1, listing(b"a") + listing(b"b")),
                                ("MAX_CI_SOURCE_FILE_BYTES", 2, listing()),
                                ("MAX_CI_SOURCE_LINK_BYTES", 2, listing(mode=b"120000")),
                                ("MAX_CI_SOURCE_TREE_BYTES", 5, listing(b"a") + listing(b"b")),
                                ("MAX_CI_SOURCE_ENTRIES", 2, listing(b"a/b"))]:
            with self.subTest(bound=name), patch.object(limits, name, cap), self.assertRaises(MbError):
                parse_source_inventory(data)

    def test_verified_copy_returns_sha256_and_compares_all_git_fields(self):
        inventory = parse_source_inventory(listing())
        record = {"path": "src/main.py", "mode": "100644", "size": 3,
                  "git_blob": "a" * 40, "sha256": hashlib.sha256(b"abc").hexdigest()}
        with patch("mod_base.build_ci.source.source_records", return_value=[record]) as reader:
            self.assertEqual(verify_source_copy(Path("sealed"), inventory=inventory), [record])
            self.assertEqual(reader.call_args.kwargs["tracked_paths"], ("src/main.py",))
        for key, value in [("path", "other"), ("mode", "100755"), ("size", 4), ("git_blob", "b" * 40)]:
            with self.subTest(field=key), patch("mod_base.build_ci.source.source_records",
                                               return_value=[{**record, key: value}]), self.assertRaises(MbError):
                verify_source_copy(Path("sealed"), inventory=inventory)

    def test_direct_inventory_cannot_bypass_parser(self):
        cases = [(), [], (object(),), (GitSourceEntry("x", "100644", True, "a" * 40),),
                 (GitSourceEntry("x", "100644", 0, "a" * 40),) * 2]
        for inventory in cases:
            with self.subTest(inventory=inventory), self.assertRaises(MbError):
                verify_source_copy(Path("sealed"), inventory=inventory)

    def test_source_declarations_are_rejected_before_any_filesystem_access(self):
        for paths, generated in [("x", ()), (("x",), "build"), (("x",), ("X/build",)),
                                 (("x",), ("x/sub",)), (("x",), (".git",)), (("x", "x"), ())]:
            with self.subTest(paths=paths, generated=generated), self.assertRaises(MbError):
                source_records(Path("unused"), tracked_paths=paths, generated_roots=generated,
                               max_files=10, max_entries=20, max_total_bytes=20,
                               max_file_bytes=10, max_link_bytes=10)

    def test_copy_publishes_only_after_original_copied_and_staged_bytes_agree(self):
        inventory = parse_source_inventory(listing())
        records = [{"path": "src/main.py", "mode": "100644", "size": 3,
                    "git_blob": "a" * 40, "sha256": "b" * 64}]
        def atomic(output, writer):
            self.assertEqual(output, Path("destination"))
            return writer(Path("stage"), 100)
        with patch("mod_base.build_ci.source.atomic_directory", side_effect=atomic), \
                patch("mod_base.build_ci.source.copy_source_files", return_value=records), \
                patch("mod_base.build_ci.source.verify_source_copy", side_effect=[records, records]):
            self.assertEqual(materialize_source_copy(Path("original"), Path("destination"),
                                                      inventory=inventory), records)
        for copied, staged in [([], records), (records, [])]:
            with self.subTest(copied=copied, staged=staged), \
                    patch("mod_base.build_ci.source.atomic_directory", side_effect=atomic), \
                    patch("mod_base.build_ci.source.copy_source_files", return_value=copied), \
                    patch("mod_base.build_ci.source.verify_source_copy", side_effect=[records, staged]), \
                    self.assertRaises(MbError):
                materialize_source_copy(Path("original"), Path("destination"), inventory=inventory)


class AuthenticatedInventoryTests(unittest.TestCase):
    def test_protected_historical_inventory_uses_subject_tree(self):
        plan, api, _, _ = self.fixture()
        identity = protected_subject(plan, api)
        inventory = authenticate_source_inventory(api, identity=identity)
        self.assertEqual([entry.path for entry in inventory], ["empty", "link", "src/main.py"])
        self.assertNotEqual(identity["tested_sha"], identity["controller_sha"])
        self.assertEqual(api.mutations, [])

    def test_non_pr_controller_movement_during_inventory_read_rejects(self):
        plan, api, _, _ = self.fixture()
        identity = protected_subject(plan, api)
        original = api.get_json
        def reading(path, **kwargs):
            value = original(path, **kwargs)
            if "/git/trees/" in path:
                api.set_branch("master", "f" * 40, "e" * 40)
            return value
        with patch.object(api, "get_json", side_effect=reading), self.assertRaises(MbError):
            authenticate_source_inventory(api, identity=identity)

    def fixture(self):
        plan, api, pr = seeded_pr()
        rows = [{"path": "src", "mode": "040000", "type": "tree", "sha": "a" * 40},
                {"path": "src/main.py", "mode": "100755", "type": "blob", "sha": "b" * 40, "size": 3},
                {"path": "empty", "mode": "100644", "type": "blob", "sha": "c" * 40, "size": 0},
                {"path": "link", "mode": "120000", "type": "blob", "sha": "d" * 40, "size": 3}]
        api.add_tree(plan["identity"]["tested_tree"], rows)
        return plan, api, pr, rows

    def test_complete_live_tree_is_read_without_candidate_execution_or_mutation(self):
        plan, api, _, _ = self.fixture()
        result = authenticate_source_inventory(api, identity=plan["identity"])
        self.assertEqual([(entry.path, entry.mode, entry.size) for entry in result],
                         [("empty", "100644", 0), ("link", "120000", 3), ("src/main.py", "100755", 3)])
        self.assertEqual(api.mutations, [])

    def test_truncation_wrong_tree_and_missing_evidence_are_fatal(self):
        for change in ("truncated", "wrong", "missing"):
            plan, api, _, _ = self.fixture()
            path = f"/repos/{api.repository}/git/trees/{plan['identity']['tested_tree']}"
            response = {"sha": "e" * 40 if change == "wrong" else plan["identity"]["tested_tree"],
                        "tree": [], "truncated": change == "truncated"}
            if change == "missing":
                response = None
            original = api.get_json
            def reading(request, **kwargs):
                return response if request == path else original(request, **kwargs)
            with self.subTest(change=change), patch.object(api, "get_json", side_effect=reading), self.assertRaises(MbError):
                authenticate_source_inventory(api, identity=plan["identity"])

    def test_unsupported_entries_size_and_directory_closure_reject(self):
        for change in ("submodule", "missing-size", "missing-directory", "extra-directory", "case-alias"):
            plan, api, _, rows = self.fixture()
            if change == "submodule":
                rows[-1].update(mode="160000", type="commit")
            elif change == "missing-size":
                rows[1].pop("size")
            elif change == "missing-directory":
                rows.pop(0)
            elif change == "extra-directory":
                rows.append({"path": "extra", "mode": "040000", "type": "tree", "sha": "e" * 40})
            else:
                rows.append({"path": "SRC/file", "mode": "100644", "type": "blob", "sha": "e" * 40, "size": 1})
            api.add_tree(plan["identity"]["tested_tree"], rows)
            with self.subTest(change=change), self.assertRaises(MbError):
                authenticate_source_inventory(api, identity=plan["identity"])

    def test_source_caps_apply_to_authenticated_api_metadata(self):
        for name, cap in [("MAX_CI_SOURCE_ENTRIES", 4), ("MAX_CI_SOURCE_FILES", 2),
                          ("MAX_CI_SOURCE_LIST_BYTES", 1), ("MAX_CI_SOURCE_TREE_BYTES", 5)]:
            plan, api, _, _ = self.fixture()
            with self.subTest(bound=name), patch.object(limits, name, cap), self.assertRaises(MbError):
                authenticate_source_inventory(api, identity=plan["identity"])

    def test_live_head_change_during_tree_read_forbids_return(self):
        plan, api, pr, _ = self.fixture()
        original = api.get_json
        def reading(path, **kwargs):
            value = original(path, **kwargs)
            if "/git/trees/" in path:
                pr["head"]["sha"] = "e" * 40
                api.add_response(f"/repos/{api.repository}/pulls/7", pr)
            return value
        with patch.object(api, "get_json", side_effect=reading), self.assertRaises(MbError):
            authenticate_source_inventory(api, identity=plan["identity"])
