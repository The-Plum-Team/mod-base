"""Newest selection remains binding inside private publication; Windows seams are explicit."""

import copy
import hashlib
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import exports, transport
from mod_base.build_ci.selection import download_latest_pr_build, wait_for_latest_pr_build
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.test_ci_transport import transport_fixture


def latest_download_fixture():
    fixture = transport_fixture()
    plan, api, _, _, run, *_ = fixture
    run["display_title"] = grammar.ci_pr_build_title(profile=plan["profile"],
        **{key: plan["identity"][key] for key in ("pr_number", "head_sha", "base_sha", "tested_sha")})
    api.add_run(run)
    return fixture


def supersede(fixture):
    fixture[1].add_run({**fixture[4], "id": 44, "created_at": "2026-10-07T11:00:00Z",
                        "status": "queued", "conclusion": None})


class LatestDownloadTests(unittest.TestCase):
    def call(self, fixture, output):
        return download_latest_pr_build(fixture[1], plan=fixture[0],
            workflow_path=".github/workflows/build-gate.yml", output=output,
            monotonic=lambda: 0, sleep=lambda _: self.fail("successful producer must not sleep"))

    def seams(self, stack, fixture, *, copying=None, verify=None):
        envelope = fixture[3]
        raw = canonical_json(envelope)
        inventory = [{key: f[key] for key in ("path", "size", "sha256")} for f in envelope["files"]]
        inventory.append({"path": grammar.CI_ENVELOPE_NAME, "size": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
        inventory.sort(key=lambda f: f["path"])
        def atomic(output, writer):
            stage = output.parent / "private-stage"
            stage.mkdir()
            try:
                result = writer(stage, 0)
                stage.rename(output)
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        def copy_files(*args, **kwargs):
            if copying:
                copying()
            return inventory
        stack.enter_context(patch.object(exports, "atomic_directory", side_effect=atomic))
        stack.enter_context(patch.object(exports, "copy_regular_files", side_effect=copy_files))
        stack.enter_context(patch.object(exports, "verify_build_export",
                                         side_effect=verify or (lambda *a, **kw: copy.deepcopy(envelope))))
        stack.enter_context(patch.object(transport, "verify_build_export", return_value=copy.deepcopy(envelope)))
        stack.enter_context(patch.object(transport, "extract_build"))

    def test_numeric_download_returns_bound_input_and_publishes_only_after_latest_admission(self):
        fixture = latest_download_fixture()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output = Path(directory) / "output"
            self.seams(stack, fixture)
            download = stack.enter_context(patch.object(fixture[1], "download", wraps=fixture[1].download))
            observed = self.call(fixture, output)
            self.assertEqual(observed, {"descriptor": fixture[2], "envelope": fixture[3]})
            self.assertTrue(output.is_dir())
            self.assertEqual(list(Path(directory).iterdir()), [output])
            download.assert_called_once_with("/repos/example/mod/actions/artifacts/100/zip",
                                             max_bytes=fixture[2]["artifact"]["size"])
        self.assertEqual(fixture[1].mutations, [])

    def test_new_producer_after_wait_is_rejected_before_download(self):
        fixture = latest_download_fixture()
        def wait(*args, **kwargs):
            result = wait_for_latest_pr_build(*args, **kwargs)
            supersede(fixture)
            return result
        with tempfile.TemporaryDirectory() as directory, \
                patch("mod_base.build_ci.selection.wait_for_latest_pr_build", side_effect=wait), \
                patch.object(fixture[1], "download") as download, self.assertRaises(MbError):
            self.call(fixture, Path(directory) / "output")
        download.assert_not_called()

    def test_newest_change_during_download_or_copy_leaves_no_published_output(self):
        for when in ("download", "copy"):
            fixture = latest_download_fixture()
            original = fixture[1].download
            def downloading(*args, **kwargs):
                data = original(*args, **kwargs)
                supersede(fixture)
                return data
            with self.subTest(when=when), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                self.seams(stack, fixture, copying=(lambda: supersede(fixture)) if when == "copy" else None)
                if when == "download":
                    stack.enter_context(patch.object(fixture[1], "download", side_effect=downloading))
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_change_digest_mismatch_and_final_stage_change_cannot_publish(self):
        for change in ("source", "digest", "stage"):
            fixture = latest_download_fixture()
            original = fixture[1].download
            def downloading(*args, **kwargs):
                data = original(*args, **kwargs)
                if change == "source":
                    fixture[1].set_branch("master", "f" * 40, "e" * 40)
                return data + b"corrupt" if change == "digest" else data
            count = [0]
            def verify(*args, **kwargs):
                count[0] += 1
                result = copy.deepcopy(fixture[3])
                if change == "stage" and count[0] == 3:
                    result["files"] = []
                return result
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                self.seams(stack, fixture, verify=verify)
                stack.enter_context(patch.object(fixture[1], "download", side_effect=downloading))
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caller_plan_mutation_during_copy_cannot_replace_retained_identity(self):
        fixture = latest_download_fixture()
        expected = copy.deepcopy(fixture[3])
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self.seams(stack, fixture, copying=fixture[0].clear)
            observed = self.call(fixture, Path(directory) / "output")
        self.assertEqual(observed["envelope"], expected)
        self.assertEqual(observed["descriptor"]["identity"], expected["identity"])
