"""The organization members snapshot (``mod-base.ci.org-members``): its schema, and the loader that
admits it for exactly one evaluation."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import mod_base
from mod_base.build_ci.membership import (ORG_MEMBERS_KIND, MembershipError, OrgMembers, load_org_members,
                                          validate_org_members)
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import MAX_DOCUMENT_BYTES, VALIDATORS, validate_document
from tests.helpers import ci_org_members

REPOSITORY = "example/mod"


class SchemaTests(unittest.TestCase):
    def test_the_kind_is_registered_at_schema_version_one(self) -> None:
        self.assertEqual(mod_base.SCHEMA_VERSIONS[ORG_MEMBERS_KIND], 1)
        self.assertIs(VALIDATORS[ORG_MEMBERS_KIND], validate_org_members)
        self.assertEqual(MAX_DOCUMENT_BYTES[ORG_MEMBERS_KIND], limits.MAX_CI_ORG_MEMBERS_BYTES)
        document = ci_org_members()
        self.assertIs(validate_document(document, kind=ORG_MEMBERS_KIND), document)

    def test_ids_are_ascending_bounded_and_the_members_are_checked(self) -> None:
        most = list(range(1, limits.MAX_CI_ADMISSION_COMMENTS + 1))
        for changes in ({"checked": [], "members": []}, {"checked": most, "members": most},
                        {"through_comment_id": 0}, {"members": []}):
            with self.subTest(valid=changes):
                validate_org_members(ci_org_members(**changes))
        for changes in ({"checked": [*most, len(most) + 1]}, {"checked": [101, 101]}, {"checked": [202, 101]},
                        {"checked": [0]}, {"checked": [True]}, {"checked": [1.5]}, {"members": [404]},
                        {"members": [303, 202]}, {"through_comment_id": -1}, {"pr_number": 0},
                        {"organization": ""}, {"organization": "a" * 40}, {"organization": "example\n"}):
            with self.subTest(invalid=changes), self.assertRaises(MbError):
                validate_org_members(ci_org_members(**changes))
        for key in ci_org_members():
            changed = ci_org_members()
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(MbError):
                validate_org_members(changed)
        with self.assertRaises(MbError):
            validate_org_members({**ci_org_members(), "logins": {}})
        with self.assertRaises(MbError):
            validate_org_members({**ci_org_members(), "schema_version": 2})


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, document: Any, name: str = "members.json", raw: bytes | None = None) -> Path:
        path = self.directory / name
        path.write_bytes(canonical_json(document) if raw is None else raw)
        return path

    def test_the_snapshot_of_this_pull_request_and_owner_is_loaded(self) -> None:
        loaded = load_org_members(self.write(ci_org_members()), repository=REPOSITORY, pr_number=7)
        self.assertEqual(loaded, OrgMembers("example", 7, 30, frozenset({101, 202, 303}), frozenset({202})))

    def test_anything_else_is_refused(self) -> None:
        document = ci_org_members()
        pretty = canonical_json(document).replace(b",", b", ")
        cases = {
            "another organization": (self.write(ci_org_members(organization="other")), {}),
            "the owner in another case": (self.write(ci_org_members(organization="Example")), {}),
            "another pull request": (self.write(document), {"pr_number": 8}),
            "bytes that are not canonical": (self.write(document, raw=pretty), {}),
            "no trailing newline": (self.write(document, raw=canonical_json(document).rstrip(b"\n")), {}),
            "a document over its bound": (self.write(document, raw=b" " * (limits.MAX_CI_ORG_MEMBERS_BYTES + 1)), {}),
            "an empty file": (self.write(document, raw=b""), {}),
            "a missing file": (self.directory / "absent.json", {}),
            "a directory": (self.directory, {}),
        }
        for label, (path, overrides) in cases.items():
            with self.subTest(case=label), self.assertRaises(MbError):
                load_org_members(path, **{"repository": REPOSITORY, "pr_number": 7, **overrides})
        with self.assertRaises(MembershipError):
            load_org_members(self.write(ci_org_members(organization="other")), repository=REPOSITORY, pr_number=7)

    @unittest.skipIf(not hasattr(os, "symlink") or os.name == "nt", "needs POSIX symlinks")
    def test_a_symlink_is_refused(self) -> None:
        target = self.write(ci_org_members())
        link = self.directory / "link.json"
        link.symlink_to(target)
        with self.assertRaises(MbError):
            load_org_members(link, repository=REPOSITORY, pr_number=7)

    def test_the_loader_validates_what_it_returns(self) -> None:
        document = copy.deepcopy(ci_org_members(members=[404]))
        with self.assertRaises(MbError):
            load_org_members(self.write(document), repository=REPOSITORY, pr_number=7)


if __name__ == "__main__":
    unittest.main()
