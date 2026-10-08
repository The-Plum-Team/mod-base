"""The batch manifest as plain data, and the marker line that carries it in a pull request body.

The valid document is ``tests.helpers.ci_batch()``: the batch Git really produces for the fixture
repository (``tests/test_ci_batch_git.py`` proves that). Everything here is pure.
"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci import batch_schema
from mod_base.build_ci.batch import batch_marker, read_batch_marker
from mod_base.errors import MbError
from mod_base.model import documents, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_batch

OPEN = "<!-- mod-base-batch "


def large(count: int, title: str = "fix: one small change to a file") -> dict:
    """A structurally valid manifest of ``count`` members with distinct ids."""

    document = ci_batch()
    prototype = document["members"][0]
    document["members"] = []
    for index in range(count):
        member = copy.deepcopy(prototype)
        member.update(pr_number=index + 1, title=title, head_sha=f"{index:04x}" + "a" * 36,
                      squash_sha=f"{index:04x}" + "b" * 36, result_tree=f"{index:04x}" + "c" * 36)
        document["members"].append(member)
    document["result_tree"] = document["members"][-1]["result_tree"]
    return document


class BatchManifestTest(unittest.TestCase):
    def reject(self, document, message):
        with self.assertRaisesRegex(MbError, message):
            batch_schema.validate_batch_manifest(document)

    def test_the_fixture_validates_loads_and_keeps_its_order(self):
        document = ci_batch()
        self.assertIs(batch_schema.validate_batch_manifest(document), document)
        self.assertEqual(load_document(canonical_json(document), kind=document["kind"]), document)
        self.assertEqual([member["pr_number"] for member in document["members"]], [1, 2])
        document["members"].reverse()
        self.reject(document, "must be the last member's result")

    def test_closed_shape_exact_types_bounds_and_versions(self):
        for target, field, value in (
                ("document", "approval", True), ("document", "schema_version", 2), ("document", "schema_version", True),
                ("document", "repository", "not a repository"), ("document", "base_sha", "F" * 40),
                ("document", "profile", "block-pops"), ("document", "policy_sha256", "1" * 64),
                ("member", "pr_number", True), ("member", "pr_number", 0), ("member", "draft", False),
                ("member", "source_repository", "example/mod"), ("member", "program", "evil.py"),
                ("member", "head_tree", "f" * 39), ("member", "patch_sha256", "f" * 40),
                ("member", "title", ""), ("member", "title", "   "), ("member", "title", "two\nlines"),
                ("member", "title", "tab\there"), ("member", "title", "\x7f"),
                ("member", "title", "t" * (limits.MAX_CI_BATCH_TITLE_CHARS + 1)), ("member", "title", 7)):
            with self.subTest(field=field, value=value):
                document = ci_batch()
                (document if target == "document" else document["members"][0])[field] = value
                with self.assertRaises(MbError):
                    batch_schema.validate_batch_manifest(document)
        for key in ("kind", "repository", "base_branch", "base_sha", "base_tree", "branch", "members", "result_tree"):
            document = ci_batch()
            del document[key]
            self.reject(document, "missing required keys")
        document = ci_batch()
        del document["members"][1]["merge_base_sha"]
        self.reject(document, "missing required keys")
        document = ci_batch()
        document["members"][0]["title"] = "t" * limits.MAX_CI_BATCH_TITLE_CHARS
        batch_schema.validate_batch_manifest(document)

    def test_between_one_and_fifty_members(self):
        batch_schema.validate_batch_manifest(large(limits.MAX_CI_BATCH_MEMBERS))
        with patch.object(batch_schema, "check", side_effect=AssertionError("linkage ran before the bounds")):
            for members in ([], large(limits.MAX_CI_BATCH_MEMBERS + 1)["members"]):
                document = ci_batch()
                document["members"] = members
                with self.subTest(count=len(members)), self.assertRaisesRegex(MbError, "between 1 and 50 items"):
                    batch_schema.validate_batch_manifest(document)

    def test_the_branch_is_a_batch_branch_and_the_base_is_not(self):
        for branch in ("feature/batch", "batch", "batch/", "batch/x.lock", "batch/a/.hidden", "batch/x.",
                       "batch/a.lock/b", "batch/a..b"):
            document = ci_batch()
            document["branch"] = branch
            with self.subTest(branch=branch), self.assertRaises(MbError):
                batch_schema.validate_batch_manifest(document)
        document = ci_batch()
        document["base_branch"] = "batch/older"
        self.reject(document, "never based on a batch branch")
        document = ci_batch()
        document.update(branch="batch/2026-10-08.1", base_branch="release/1.x")
        batch_schema.validate_batch_manifest(document)

    def test_what_the_shape_alone_can_contradict(self):
        first, second = ci_batch()["members"]
        for index, field, value, message in (
                (1, "pr_number", first["pr_number"], "duplicate batch member"),
                (1, "head_sha", first["head_sha"], "two members share one head"),
                (0, "merge_base_sha", first["head_sha"], "needs commits of its own"),
                (0, "squash_sha", first["head_sha"], "a new commit of this batch"),
                (0, "squash_sha", second["head_sha"], "a new commit of this batch"),
                (1, "squash_sha", ci_batch()["base_sha"], "a new commit of this batch"),
                (1, "squash_sha", first["squash_sha"], "a new commit of this batch"),
                (0, "result_tree", ci_batch()["base_tree"], "a member must change the stack"),
                (1, "result_tree", first["result_tree"], "a member must change the stack")):
            with self.subTest(index=index, field=field):
                document = ci_batch()
                document["members"][index][field] = value
                document["result_tree"] = document["members"][-1]["result_tree"]
                self.reject(document, message)
        document = ci_batch()
        document["members"][1]["merge_base_sha"] = "9" * 40
        document["members"][0]["squash_sha"] = "9" * 40
        self.reject(document, "a new commit of this batch")
        document = ci_batch()
        document["result_tree"] = first["result_tree"]
        self.reject(document, "must be the last member's result")

    def test_strict_decoder_duplicates_nan_and_the_body_sized_document_bound(self):
        raw = canonical_json(ci_batch())
        for bad in (raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
                    raw.replace(b'"schema_version":1', b'"schema_version":NaN'), b"\xef\xbb\xbf" + raw):
            with self.assertRaises(MbError):
                load_document(bad, kind="mod-base.ci.batch")
        self.assertEqual(documents.MAX_DOCUMENT_BYTES["mod-base.ci.batch"], 65536)
        self.assertEqual(limits.MAX_CI_BATCH_DOCUMENT_BYTES, 65536)
        with self.assertRaisesRegex(MbError, "65536-byte input limit"):
            load_document(raw + b" " * 65536, kind="mod-base.ci.batch")


class BatchMarkerTest(unittest.TestCase):
    def test_the_marker_is_one_ascii_comment_line_that_reads_back(self):
        document = ci_batch()
        document["members"][0]["title"] = 'feat: "café" \\ <!-- mod-base-batch {} --> & \U0001f600   | x'
        marker = batch_marker(document)
        self.assertTrue(marker.startswith(OPEN) and marker.endswith(" -->"))
        self.assertTrue(marker.isascii() and marker.isprintable())
        self.assertEqual((marker.count("<"), marker.count(">"), marker.count("&"), marker.count("\n")), (1, 1, 0, 0))
        self.assertIn("\\u003c!-- mod-base-batch {} --\\u003e \\u0026 \\ud83d\\ude00 \\u2028", marker)
        for body in (marker, marker + "\n", f"## Summary\n\ntext\n\n{marker}\n", f"text\r\n{marker}\r\ntrailer\r\n"):
            with self.subTest(body=body[:20]):
                self.assertEqual(read_batch_marker(body), document)
        self.assertEqual(read_batch_marker(batch_marker(ci_batch())), ci_batch())

    def test_fifty_members_with_ordinary_titles_fit_the_body_bound(self):
        marker = batch_marker(large(limits.MAX_CI_BATCH_MEMBERS, "fix: an ordinary title of a pull request, sixty characters."))
        self.assertLess(len(marker), limits.MAX_CI_BATCH_DOCUMENT_BYTES // 2)
        self.assertEqual(len(read_batch_marker("intro\n" + marker + "\n")["members"]), 50)

    def test_only_one_whole_canonical_marker_line_is_read(self):
        marker = batch_marker(ci_batch())
        text = marker[len(OPEN):-len(" -->")]
        for body, message in (
                (None, "no body"), (b"bytes", "no body"), ("", "no single batch marker"),
                ("## Summary\n", "no single batch marker"), (marker + "\n" + marker, "no single batch marker"),
                ("text " + marker, "not one whole line"), (marker + " trailing", "not one whole line"),
                (marker[:-4], "not one whole line"), (marker.replace(" -->", "-->"), "not one whole line"),
                (OPEN + text + "  -->", "not valid JSON|canonical spelling|not one whole line"),
                (OPEN + " " + text + " -->", "canonical spelling"),
                (OPEN + text.replace('"schema_version":1', '"schema_version": 1') + " -->", "canonical spelling"),
                (OPEN + text.replace("fix: change fix/alpha", "fix: change fix\\/alpha") + " -->", "canonical spelling"),
                (OPEN + text.replace("fix: change fix/alpha", "café") + " -->", "not one whole line"),
                (OPEN + text[:-1] + ',"approval":true} -->', "unknown keys"),
                (OPEN + text.replace('"pr_number":2', '"pr_number":1') + " -->", "duplicate batch member"),
                (OPEN + "{} -->", "missing required keys"), (OPEN + "[] -->", "must be an object"),
                (OPEN + "not json -->", "not valid JSON"),
                (marker + "\n" + "x" * limits.MAX_CI_BATCH_DOCUMENT_BYTES, "no body")):
            with self.subTest(message=message, body=str(body)[:30]), self.assertRaisesRegex(MbError, message):
                read_batch_marker(body)


if __name__ == "__main__":
    unittest.main()
