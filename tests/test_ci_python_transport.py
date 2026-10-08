"""Pinned publisher admission using seeded metadata and independently authored GNU bytes."""

import copy
import gzip
import hashlib
import unittest
from unittest.mock import patch

from mod_base.build_ci import python_transport as transport
from mod_base.build_ci.worker import WorkerError
from mod_base.github.api import ApiNotFound, RequestBudgetExhausted
from mod_base.github.fake import FakeGitHub
from mod_base.model import limits
from tests.test_ci_python_archive import ROWS, tar_bytes


class PythonTransportTests(unittest.TestCase):
    def fixture(self, version="3.11.17"):
        data = gzip.compress(tar_bytes(ROWS), mtime=0)
        digest = hashlib.sha256(data).hexdigest()
        release_id, asset_id, run_id, commit = transport._PUBLISHERS[version]
        prefix = "/repos/actions/python-versions"
        tag = f"{version}-{run_id}"
        asset = {"id": asset_id, "name": f"python-{version}-linux-24.04-x64.tar.gz",
                 "state": "uploaded", "size": len(data), "digest": f"sha256:{digest}"}
        run = {"id": run_id, "run_attempt": 1, "event": "workflow_dispatch", "status": "completed",
               "conclusion": "success", "path": ".github/workflows/build-python-packages.yml",
               "head_branch": "main", "head_sha": commit,
               "repository": {"full_name": "actions/python-versions"},
               "head_repository": {"full_name": "actions/python-versions"}}
        records = {
            f"{prefix}/releases/{release_id}": {"id": release_id, "tag_name": tag, "draft": False,
                                                 "prerelease": False, "assets": [asset]},
            f"{prefix}/releases/assets/{asset_id}": asset,
            f"{prefix}/git/ref/tags/{tag}": {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": commit}},
            f"{prefix}/git/commits/{commit}": {"sha": commit},
            f"{prefix}/actions/runs/{run_id}": run,
            f"{prefix}/actions/runs/{run_id}/attempts/1": run,
        }
        api = FakeGitHub(repository="actions/python-versions")
        for path, record in records.items():
            api.add_response(path, record)
        api.add_release_asset("actions/python-versions", asset_id, data)
        return api, records, data, {version: (len(data), digest)}

    def test_each_profile_brackets_real_inert_reader(self):
        for version in transport._PUBLISHERS:
            with self.subTest(version=version):
                api, _, data, profiles = self.fixture(version)
                # Fixture approval is derived from authored bytes before the transport is called.
                with patch.object(transport, "_PROFILES", profiles):
                    self.assertEqual(data, transport.download_python_installer(api, version=version))
                self.assertEqual(13, api.request_count)

    def test_every_relied_metadata_field_rejects_before_download(self):
        _, originals, _, _ = self.fixture()
        for path, record in originals.items():
            for key, value in record.items():
                if key == "assets":
                    continue
                for wrong in (None, True, 0, "wrong"):
                    if type(wrong) is type(value) and wrong == value:
                        continue
                    with self.subTest(path=path, key=key, wrong=wrong):
                        api, _, _, profiles = self.fixture()
                        changed = copy.deepcopy(record)
                        changed[key] = wrong
                        api.add_response(path, changed)
                        with patch.object(transport, "_PROFILES", profiles), patch.object(api, "download_release_asset") as download:
                            with self.assertRaises(WorkerError):
                                transport.download_python_installer(api, version="3.11.17")
                            download.assert_not_called()

    def test_membership_duplicates_missing_and_bound(self):
        api, records, _, profiles = self.fixture()
        path = next(iter(records))
        release = records[path]
        selected = release["assets"][0]
        for assets in (None, [], [selected, selected], [{**selected, "id": True}],
                       [{**selected, "id": selected["id"] + 1}],
                       [selected] * (limits.MAX_CI_PYTHON_RELEASE_ASSETS + 1)):
            with self.subTest(assets=assets):
                api.add_response(path, {**release, "assets": assets})
                with patch.object(transport, "_PROFILES", profiles), self.assertRaises(WorkerError):
                    transport.download_python_installer(api, version="3.11.17")

    def test_nested_tag_repository_and_asset_membership_fields(self):
        _, records, _, _ = self.fixture()
        mutations = []
        for path, record in records.items():
            for key in ("object", "repository", "head_repository"):
                if key in record:
                    for field in record[key]:
                        mutations.append((path, {**record, key: {**record[key], field: "wrong"}}))
            if "assets" in record:
                for field in record["assets"][0]:
                    asset = {**record["assets"][0], field: None}
                    mutations.append((path, {**record, "assets": [asset]}))
        for path, record in mutations:
            with self.subTest(path=path, record=record):
                api, _, _, profiles = self.fixture()
                api.add_response(path, record)
                with patch.object(transport, "_PROFILES", profiles), self.assertRaises(WorkerError):
                    transport.download_python_installer(api, version="3.11.17")

    def test_payload_tampering_and_metadata_drift_do_not_return_bytes(self):
        api, records, data, profiles = self.fixture()
        asset_id = transport._PUBLISHERS["3.11.17"][1]
        api.add_release_asset("actions/python-versions", asset_id, data[:-1] + bytes([data[-1] ^ 1]))
        with patch.object(transport, "_PROFILES", profiles), self.assertRaises(WorkerError):
            transport.download_python_installer(api, version="3.11.17")
        self.assertEqual(7, api.request_count)
        api, records, data, profiles = self.fixture()
        original = api.download_release_asset

        def mutate(repository, asset_id, *, max_bytes):
            result = original(repository, asset_id, max_bytes=max_bytes)
            path = next(iter(records))
            api.add_response(path, {**records[path], "draft": True})
            return result

        with patch.object(transport, "_PROFILES", profiles), patch.object(api, "download_release_asset", mutate):
            with self.assertRaises(WorkerError):
                transport.download_python_installer(api, version="3.11.17")

    def test_unknown_profile_and_api_errors_fail_closed(self):
        api = FakeGitHub(repository="actions/python-versions")
        for version in (None, True, "3.12.10", "3.11", "3.11.17/extra"):
            with self.subTest(version=version), self.assertRaises(WorkerError):
                transport.download_python_installer(api, version=version)
        self.assertEqual(0, api.request_count)
        with self.assertRaises(ApiNotFound):
            transport.download_python_installer(api, version="3.11.17")
        api, _, _, profiles = self.fixture()
        api._max_requests = 6
        with patch.object(transport, "_PROFILES", profiles), self.assertRaises(RequestBudgetExhausted):
            transport.download_python_installer(api, version="3.11.17")
