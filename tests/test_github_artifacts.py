"""``github.artifacts``: the strict artifact model (Quick Skin ``rotate_artifacts.Artifact``), exact
bounded listings, exact-ID deletion and the verified download, ported from the transport part of
Block Pops ``tests/test_release_evidence_download.py``: exact id/size/digest reach the real
extractor; wrong digests, short and oversized transports, hostile ZIPs (symlinks, ratio > 200,
encrypted entries, traversal) and credential-bearing redirects leave no output behind."""

from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.github import api as github_api
from mod_base.github import artifacts
from mod_base.github.api import ApiError, ApiNotFound, GitHubApi, ReadOnlyViolation
from mod_base.github.artifacts import Artifact
from mod_base.github.fake import FakeGitHub
from mod_base.io.bounded_zip import LIMITS_BY_KIND, ExtractionLimits, archive_limit
from mod_base.model import grammar
from tests.helpers import COMMIT, CREATED_AT, REPOSITORY
from tests.test_github_api import Opener, Response, http_error
from tests.test_io_bounded_zip import archive, archive_with, entry, patch_central

NAME = grammar.handoff_name("mc1.20.1", 1)
RUN_ID = 101
LIMITS = LIMITS_BY_KIND["handoff"]


def record(artifact_id: int, name: str, data: bytes, *, run_id: int = RUN_ID, created_at: str = CREATED_AT,
           **changes: Any) -> dict[str, Any]:
    value = {"id": artifact_id, "node_id": "x", "name": name, "size_in_bytes": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "expired": False, "created_at": created_at,
             "expires_at": "2026-09-26T10:00:00Z", "updated_at": created_at,
             "workflow_run": {"id": run_id, "repository_id": 1, "head_repository_id": 1, "head_branch": "master",
                              "head_sha": COMMIT}}
    value.update(changes)
    return value


class ArtifactModelTests(unittest.TestCase):
    def test_parses_the_api_record_and_orders_by_time_then_id(self) -> None:
        parsed = Artifact.parse(record(7, NAME, b"zip"))
        self.assertEqual(Artifact(id=7, name=NAME, size=3, expired=False, created_at=CREATED_AT,
                                  digest="sha256:" + hashlib.sha256(b"zip").hexdigest(), run_id=RUN_ID,
                                  head_branch="master", head_sha=COMMIT), parsed)
        self.assertEqual((datetime(2026, 9, 25, 10, tzinfo=timezone.utc), 7), parsed.order)
        later = Artifact.parse(record(3, NAME, b"zip", created_at="2026-09-25T10:00:01Z"))
        self.assertLess(parsed.order, later.order)

    def test_every_field_is_strictly_validated(self) -> None:
        workflow_run = record(1, NAME, b"x")["workflow_run"]
        cases = {
            "id bool": {"id": True}, "id zero": {"id": 0}, "id text": {"id": "7"},
            "size zero": {"size_in_bytes": 0}, "size float": {"size_in_bytes": 1.0},
            "no digest": {"digest": None}, "sha1 digest": {"digest": "sha1:" + "0" * 40},
            "upper digest": {"digest": "sha256:" + "A" * 64},
            "expired text": {"expired": "false"}, "expired missing": {"expired": None},
            "created local": {"created_at": "2026-09-25T10:00:00"}, "created junk": {"created_at": "today"},
            "name empty": {"name": ""}, "name long": {"name": "x" * 241}, "name control": {"name": "a\nb"},
            "no workflow run": {"workflow_run": None},
            "run bool": {"workflow_run": {**workflow_run, "id": True}},
            "head sha": {"workflow_run": {**workflow_run, "head_sha": "HEAD"}},
            "head branch": {"workflow_run": {**workflow_run, "head_branch": None}},
        }
        for label, changes in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(MbError, "artifact"):
                Artifact.parse({**record(1, NAME, b"x"), **changes})
        self.assertRaises(MbError, Artifact.parse, [record(1, NAME, b"x")])


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY)
        for index in range(130):
            self.api.add_artifact(record(1000 + index, NAME, b"z", run_id=RUN_ID + index % 3,
                                         created_at=f"2026-09-25T10:{index // 60:02d}:{index % 60:02d}Z"), b"z")
        self.api.add_artifact(record(5000, grammar.collected_name("mc1.20.1"), b"c", run_id=RUN_ID), b"c")

    def test_list_named_is_exact_complete_and_newest_first(self) -> None:
        listed = artifacts.list_named(self.api, NAME)
        self.assertEqual(130, len(listed))
        self.assertEqual({NAME}, {item.name for item in listed})
        self.assertEqual(sorted(listed, key=lambda item: item.order, reverse=True), listed)
        self.assertEqual(2, self.api.request_count)

    def test_list_named_refuses_foreign_names_and_rows(self) -> None:
        for name in ("pages-cache-master", "mb-handoff--MC--a1", "", "github-pages2"):
            with self.subTest(name=name), self.assertRaisesRegex(MbError, "not a kit artifact name"):
                artifacts.list_named(self.api, name)
        api = FakeGitHub(repository=REPOSITORY)
        api.add_response(f"/repos/{REPOSITORY}/actions/artifacts",
                         {"total_count": 1, "artifacts": [record(1, "mb-promotion", b"x")]},
                         params={"name": NAME, "per_page": 100, "page": 1})
        with self.assertRaisesRegex(MbError, "returned another name"):
            artifacts.list_named(api, NAME)
        with self.assertRaisesRegex(ApiError, "beyond its bound"):
            artifacts.list_named(self.api, NAME, max_items=100)

    def test_list_for_run_requires_the_owner_run(self) -> None:
        listed = artifacts.list_for_run(self.api, RUN_ID)
        self.assertEqual({RUN_ID}, {item.run_id for item in listed})
        self.assertIn(5000, {item.id for item in listed})
        api = FakeGitHub(repository=REPOSITORY)
        api.add_response(f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}/artifacts",
                         {"total_count": 1, "artifacts": [record(1, NAME, b"x", run_id=RUN_ID + 1)]},
                         params={"per_page": 100, "page": 1})
        with self.assertRaisesRegex(MbError, "another run"):
            artifacts.list_for_run(api, RUN_ID)

    def test_list_repository_is_bounded_and_rejects_repeated_ids(self) -> None:
        self.assertEqual(131, len(artifacts.list_repository(self.api, max_items=200)))
        with self.assertRaisesRegex(ApiError, "beyond its bound"):
            artifacts.list_repository(self.api, max_items=130)
        api = FakeGitHub(repository=REPOSITORY)
        api.add_response(f"/repos/{REPOSITORY}/actions/artifacts",
                         {"total_count": 2, "artifacts": [record(1, NAME, b"x"), record(1, NAME, b"x")]},
                         params={"per_page": 100, "page": 1})
        with self.assertRaisesRegex(MbError, "repeats"):
            artifacts.list_repository(api, max_items=10)

    def test_get_artifact_requires_the_exact_id(self) -> None:
        self.assertEqual(1000, artifacts.get_artifact(self.api, 1000).id)
        self.api.add_response(f"/repos/{REPOSITORY}/actions/artifacts/77", record(78, NAME, b"x"))
        with self.assertRaisesRegex(MbError, "another artifact"):
            artifacts.get_artifact(self.api, 77)
        with self.assertRaises(ApiNotFound):
            artifacts.get_artifact(self.api, 79)


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "output"
        self.raw = archive({"manifest.json": b"{}", "runtime/result.json": b'{"ok":true}'})
        self.api = FakeGitHub(repository=REPOSITORY)
        self.api.add_artifact(record(7, NAME, self.raw), self.raw)

    def arguments(self, **changes: Any) -> dict[str, Any]:
        return {"artifact_id": 7, "name": NAME, "digest": "sha256:" + hashlib.sha256(self.raw).hexdigest(),
                "size": len(self.raw), "run_id": RUN_ID, "output": self.output, "extraction": LIMITS, **changes}

    def assert_clean(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual([], [path.name for path in self.root.iterdir()])

    def seed(self, data: bytes, **changes: Any) -> dict[str, Any]:
        self.raw = data
        self.api.add_artifact(record(7, NAME, data, **changes), data)
        return self.arguments()

    def test_exact_id_size_digest_reach_the_real_extractor(self) -> None:
        self.assertEqual(["manifest.json", "runtime/result.json"], artifacts.download(self.api, **self.arguments()))
        self.assertEqual(b'{"ok":true}', (self.output / "runtime/result.json").read_bytes())
        self.assertEqual(3, self.api.request_count)  # metadata, the API redirect, the storage body

    def test_metadata_must_equal_the_selection_before_any_byte_is_fetched(self) -> None:
        other = hashlib.sha256(b"other").hexdigest()
        for label, changes in {"name": {"name": grammar.handoff_name("mc1.20.1", 2)},
                               "digest": {"digest": "sha256:" + other}, "size": {"size": len(self.raw) + 1},
                               "owner run": {"run_id": RUN_ID + 1}}.items():
            with self.subTest(label=label):
                api = FakeGitHub(repository=REPOSITORY)
                api.add_artifact(record(7, NAME, self.raw), self.raw)
                with self.assertRaisesRegex(MbError, "differs from the selected artifact"):
                    artifacts.download(api, **self.arguments(**changes))
                self.assertEqual(1, api.request_count)
                self.assert_clean()
        self.api.add_artifact(record(7, NAME, self.raw, expired=True), self.raw)
        with self.assertRaisesRegex(MbError, "expired"):
            artifacts.download(self.api, **self.arguments())
        self.assert_clean()

    def test_wrong_digest_short_and_oversized_transports_fail_without_output(self) -> None:
        forged = record(7, NAME, self.raw, digest="sha256:" + "0" * 64)
        self.api.add_artifact(forged, self.raw)
        with self.assertRaisesRegex(MbError, "do not match their sha256 digest"):
            artifacts.download(self.api, **self.arguments(digest=forged["digest"]))
        self.assert_clean()
        for label, served in (("short", self.raw[:-1]), ("oversized", self.raw + b"\0")):
            with self.subTest(label=label):
                self.api.add_artifact(record(7, NAME, self.raw), served)
                with self.assertRaises(MbError):
                    artifacts.download(self.api, **self.arguments())
                self.assert_clean()

    def test_invalid_external_inputs_fail_before_any_request(self) -> None:
        for changes in ({"artifact_id": True}, {"artifact_id": 1.5}, {"artifact_id": 0}, {"size": True},
                        {"size": 0}, {"size": artifacts.MAX_ARCHIVE_BYTES + 1}, {"run_id": 0},
                        {"digest": "malformed"}, {"digest": "sha256:" + "A" * 64}, {"name": "pages-cache-master"},
                        {"name": "../mb-handoff"}, {"extraction": None}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                artifacts.download(self.api, **self.arguments(**changes))
        self.output.mkdir()
        with self.assertRaisesRegex(MbError, "must not exist"):
            artifacts.download(self.api, **self.arguments())
        self.output.rmdir()
        self.output.symlink_to(self.root / "elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(MbError, "must not exist"):
            artifacts.download(self.api, **self.arguments())
        self.output.unlink()
        self.assertEqual(0, self.api.request_count)

    def test_hostile_archives_are_rejected_without_output(self) -> None:
        hostile = {
            "symlink": archive_with([(entry("manifest.json", mode=stat.S_IFLNK | 0o777), b"/etc/passwd")]),
            "fifo": archive_with([(entry("runtime/fifo.json", mode=stat.S_IFIFO | 0o600), b"x")]),
            "ratio above 200": archive({"runtime/bomb.json": b"\0" * 500_000}),
            "encrypted": patch_central(archive({"manifest.json": b'{"secret":1}' * 4}), flags=0x1),
            "traversal": archive({"../escape.json": b"{}"}),
            "unapproved suffix": archive({"runtime/run.sh": b"#!/bin/sh"}),
            "bzip2": patch_central(archive({"manifest.json": b"{}" * 20}), method=zipfile.ZIP_BZIP2),
        }
        for label, data in hostile.items():
            with self.subTest(label=label):
                self.api = FakeGitHub(repository=REPOSITORY)
                arguments = self.seed(data)
                with self.assertRaisesRegex(MbError, "archive|unsafe|suffix|encrypted|compression"):
                    artifacts.download(self.api, **arguments)
                self.assert_clean()
        self.assertFalse((self.root.parent / "escape.json").exists())

    def test_a_size_beyond_the_kind_archive_bound_is_refused_before_any_request(self) -> None:
        cache = LIMITS_BY_KIND["cache"]
        bound = archive_limit(cache)
        self.assertLess(bound, artifacts.MAX_ARCHIVE_BYTES)
        name = grammar.cache_name("mc1.20.1", COMMIT)
        family = LIMITS_BY_KIND["collected-family"]
        # The download cap binds every kind whose archive bound reaches beyond it; the kind's expanded
        # total stays within that cap, so ``family collect`` refuses larger files before upload.
        self.assertLessEqual(family.max_total_bytes, artifacts.MAX_ARCHIVE_BYTES)
        for kind, extraction, size, artifact_name in (("cache", cache, bound + 1, name),
                                                      ("promotion", LIMITS_BY_KIND["promotion"],
                                                       archive_limit(LIMITS_BY_KIND["promotion"]) + 1, "mb-promotion"),
                                                      ("collected-family", family,
                                                       min(artifacts.MAX_ARCHIVE_BYTES, archive_limit(family)) + 1,
                                                       grammar.collected_family_name("mod-compatibility", "mc1.20.1"))):
            with self.subTest(kind=kind), self.assertRaisesRegex(MbError, "artifact size"):
                artifacts.download(self.api, **self.arguments(name=artifact_name, size=size, extraction=extraction))
        with self.assertRaisesRegex(MbError, "extraction ratio"):
            artifacts.download(self.api, **self.arguments(extraction=ExtractionLimits(8, 1024, 1024, max_ratio=500)))
        self.assertEqual(0, self.api.request_count)
        self.assert_clean()

    def test_custom_extraction_limits_are_applied(self) -> None:
        tight = ExtractionLimits(max_entries=1, max_total_bytes=1024, max_entry_bytes=1024)
        with self.assertRaises(MbError):
            artifacts.download(self.api, **self.arguments(extraction=tight))
        self.assert_clean()


class CredentialSafeTransportTests(unittest.TestCase):
    """The real client end to end: the storage redirect never receives the bearer credential."""

    STORAGE = "https://productionresultssa0.blob.core.windows.test/actions-results/7.zip?sig=abc"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name).resolve() / "output"
        self.raw = archive({"manifest.json": b"{}"})
        self.metadata = record(7, NAME, self.raw)

    def client(self, outcomes: list[Any]) -> tuple[GitHubApi, Opener]:
        opener = Opener(outcomes)
        with patch.object(github_api, "_opener", return_value=opener):
            return GitHubApi(repository=REPOSITORY, token="fixture-token", sleep=lambda _: None), opener

    def download(self, client: GitHubApi) -> list[str]:
        return artifacts.download(client, artifact_id=7, name=NAME, digest=self.metadata["digest"],
                                  size=len(self.raw), run_id=RUN_ID, output=self.output, extraction=LIMITS)

    def metadata_response(self) -> Response:
        return Response(body=json.dumps(self.metadata).encode())

    def test_storage_request_carries_no_authorization(self) -> None:
        client, opener = self.client([self.metadata_response(), http_error(302, b"", {"Location": self.STORAGE}),
                                      Response(body=self.raw)])
        self.assertEqual(["manifest.json"], self.download(client))
        metadata, zip_request, storage = opener.requests
        self.assertEqual(f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/7", metadata.full_url)
        self.assertEqual(f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/7/zip", zip_request.full_url)
        self.assertEqual("Bearer fixture-token", zip_request.get_header("Authorization"))
        self.assertEqual(self.STORAGE, storage.full_url)
        self.assertIsNone(storage.get_header("Authorization"))

    def test_a_redirect_carrying_credentials_is_refused_without_output(self) -> None:
        for location in ("https://user:token@storage.test/7.zip", "http://storage.test/7.zip"):
            with self.subTest(location=location):
                client, opener = self.client([self.metadata_response(), http_error(302, b"", {"Location": location})])
                with self.assertRaisesRegex(ApiError, "unsafe location"):
                    self.download(client)
                self.assertEqual(2, len(opener.requests))
                self.assertFalse(self.output.exists())


class DeleteTests(unittest.TestCase):
    def test_delete_is_exact_and_needs_a_writable_client(self) -> None:
        read_only = FakeGitHub(repository=REPOSITORY)
        read_only.add_artifact(record(7, NAME, b"z"), b"z")
        with self.assertRaises(ReadOnlyViolation):
            artifacts.delete(read_only, 7)
        self.assertEqual(0, read_only.request_count)
        writable = FakeGitHub(repository=REPOSITORY, writable=True)
        writable.add_artifact(record(7, NAME, b"z"), b"z")
        artifacts.delete(writable, 7)
        self.assertEqual([7], writable.deleted_artifact_ids)
        with self.assertRaises(ApiNotFound):
            artifacts.delete(writable, 7)
        for bad in (0, True, "7"):
            with self.subTest(artifact_id=bad), self.assertRaises(MbError):
                artifacts.delete(writable, bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
