"""The original complete Build bytes of a merged pull request are copied under coherent gate proof.

Real archives and the kit's own extraction, hashing and atomic copy in a temporary directory
(Linux); only the GitHub API is fake. Where a case needs another actor at an exact point of the
command, the real function runs unchanged and the actor moves right after it returns.
"""

import copy
import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import exports, transport
from mod_base.build_ci.exports import verify_build_export
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.test_ci_merged_gate_pair import merged_pair_world, pair_arguments
from tests.test_ci_merged_gate_transport import PULL

MOVED = "changed between the start of the command and its effect"
#: What a merged pair observes again before anything is published: the merged pull request (3
#: reads), both runs' latest attempts, both seals, the bundle and the packaged run's sources.
FINAL_READS = sorted(["", "/branches/master", "/pulls/7", "/actions/runs/42", "/actions/runs/43",
                      "/actions/artifacts/200", "/actions/artifacts/201", "/actions/artifacts/100",
                      "/actions/runs/43/artifacts"])


@contextmanager
def after_call(owner, name, action, *, count=1):
    """Run ``action(*args)`` right after the ``count``-th call of ``owner.name`` returns: another
    actor moving the filesystem or the API at that exact point. The real function runs unchanged."""

    original, seen = getattr(owner, name), [0]

    def call(*args, **kwargs):
        result = original(*args, **kwargs)
        seen[0] += 1
        if seen[0] == count:
            action(*args)
        return result

    with patch.object(owner, name, side_effect=call) as calls:
        yield calls


@contextmanager
def at_final_read(api, action, *, downloads):
    """Run ``action`` once, at the first API read after the ``downloads``-th artifact download: the
    command observing its mutable state again, with everything it will publish already staged."""

    read, download, state = api.get_json, api.download, {"downloads": 0, "done": False}

    def downloading(path, **kwargs):
        data = download(path, **kwargs)
        state["downloads"] += 1
        return data

    def reading(path, **kwargs):
        if state["downloads"] == downloads and not state["done"]:
            state["done"] = True
            action()
        return read(path, **kwargs)

    with patch.object(api, "download", side_effect=downloading), patch.object(api, "get_json", side_effect=reading):
        yield state


@contextmanager
def recorded(api, output):
    """Every API read and download of a command, each with whether ``output`` existed by then."""

    events = []

    def spy(kind, original):
        def call(path, **kwargs):
            events.append((kind, path.removeprefix(f"/repos/{api.repository}"), os.path.lexists(output)))
            return original(path, **kwargs)
        return call

    with patch.object(api, "get_json", side_effect=spy("read", api.get_json)), \
            patch.object(api, "download", side_effect=spy("download", api.download)):
        yield events


def downloads_and_final_reads(events):
    """The artifact ids downloaded, in order, and the sorted reads made after the last download."""

    downloads = [index for index, event in enumerate(events) if event[0] == "download"]
    return ([int(events[index][1].split("/")[-2]) for index in downloads],
            sorted(path for _, path, _ in events[downloads[-1] + 1:]))


def stage_of(root, stage_fd):
    """The private stage a copy of the extracted ``root`` was handed as a descriptor: the directory
    next to that extraction which is the descriptor's own."""

    return next(path for path in root.parent.parent.iterdir()
                if os.path.samestat(os.stat(path), os.fstat(stage_fd)))


def tamper(path, data=None):
    """Change a file's bytes in place: the same file and mode, the same size unless ``data`` is given."""

    path.write_bytes(bytes(byte ^ 1 for byte in path.read_bytes()) if data is None else data)


def replace_payload(root, envelope, envelope_name, path):
    """Replace one payload of an export and its inventory entry together, so that the tree still
    verifies: as another export. Returns the envelope the tree carries from then on."""

    document = copy.deepcopy(envelope)
    data = b"self-consistent replacement"
    next(file for file in document["files"] if file["path"] == path).update(
        size=len(data), sha256=hashlib.sha256(data).hexdigest())
    tamper(root / path, data)
    tamper(root / envelope_name, canonical_json(document))
    return document


def merged_at(world):
    world.pr["merged_at"] = "2026-10-08T10:01:00Z"
    world.api.add_response(PULL, world.pr)


#: Mutable state of a merged pair that can move while its bytes are being copied.
MOVEMENTS = {"merged_at": merged_at,
             "controller": lambda world: world.api.set_branch("master", "f" * 40, "e" * 40),
             "build attempt": lambda world: world.set_run(42, run_attempt=3, status="queued", conclusion=None),
             "packaged attempt": lambda world: world.set_run(43, run_attempt=3)}


class MergedBuildTests(unittest.TestCase):
    def call(self, world, output):
        return transport.download_merged_build(world.api, **pair_arguments(world, output=output))

    def test_original_complete_payload_bytes_and_envelope_publish_after_paired_recheck(self):
        world = merged_pair_world()
        before = copy.deepcopy((world.plan, world.seals))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with recorded(world.api, output) as events:
                self.assertEqual(self.call(world, output), world.envelope)
            for file in world.envelope["files"]:
                self.assertEqual(hashlib.sha256((output / file["path"]).read_bytes()).hexdigest(), file["sha256"])
            self.assertEqual((output / grammar.CI_ENVELOPE_NAME).read_bytes(), canonical_json(world.envelope))
            self.assertEqual(verify_build_export(output, plan=world.plan), world.envelope)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["output"])
        # Each seal is fetched once and then the bundle; the pair is observed again, as metadata,
        # after the bundle bytes arrived, and nothing exists at the output before the last read.
        downloads, final = downloads_and_final_reads(events)
        self.assertEqual(downloads, [200, 201, 100])
        self.assertEqual(final, FINAL_READS)
        self.assertFalse(any(published for *_, published in events))
        self.assertEqual(world.api.request_count, 30)  # the pair's 28 and the bundle download (2)
        self.assertEqual((world.plan, world.seals), before)
        self.assertEqual(world.api.mutations, [])

    def test_preexisting_output_or_nonpath_rejects_before_api(self):
        world = merged_pair_world()
        with tempfile.TemporaryDirectory() as directory:
            for output in (Path(directory), False, str(Path(directory) / "output")):
                with self.subTest(output=output), patch.object(world.api, "get_json") as reads, \
                        self.assertRaises(MbError):
                    self.call(world, output)
                reads.assert_not_called()

    def test_corrupt_payload_zip_rejects_before_extraction_and_output(self):
        for size in (5, None):
            world = merged_pair_world()
            world.api.add_artifact(world.records[100], b"x" * (size or len(world.archives[100])))
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory, \
                    patch.object(transport, "extract_build", wraps=transport.extract_build) as extract:
                with self.assertRaisesRegex(MbError, "download length or SHA-256 differs"):
                    self.call(world, Path(directory) / "output")
                extract.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_and_staged_byte_tampering_reject_actual_hash_verification(self):
        for staged in (False, True):
            world = merged_pair_world()
            name = world.envelope["files"][0]["path"]
            hook = (after_call(exports, "copy_regular_files",
                               lambda root, stage_fd: tamper(stage_of(root, stage_fd) / name))
                    if staged else after_call(transport, "extract_build", lambda data, root: tamper(root / name)))
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as directory, hook as calls:
                with self.assertRaisesRegex(MbError, "frozen export differs from its exact file inventory"):
                    self.call(world, Path(directory) / "output")
                calls.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_either_full_seal_expiry_inside_copy_forbids_atomic_publication(self):
        for artifact_id in (201, 200, 100):
            world = merged_pair_world()
            with self.subTest(artifact_id=artifact_id), tempfile.TemporaryDirectory() as directory, \
                    after_call(exports, "copy_regular_files",
                               lambda *_: world.set_artifact(artifact_id, expired=True)) as copies:
                with self.assertRaisesRegex(MbError, "artifact availability " + MOVED):
                    self.call(world, Path(directory) / "output")
                copies.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_historical_source_or_attempt_change_inside_copy_cannot_publish(self):
        for name, change in MOVEMENTS.items():
            world = merged_pair_world()
            with self.subTest(change=name), tempfile.TemporaryDirectory() as directory, \
                    after_call(exports, "copy_regular_files", lambda *_: change(world)) as copies:
                with self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                copies.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caller_documents_are_copied_on_entry(self):
        world = merged_pair_world()
        admitted = copy.deepcopy(world.plan)

        def substitute(*_):
            world.plan["identity"]["kit"]["sha"] = "f" * 40
            for seal in world.seals.values():
                seal["artifact"]["digest"] = "sha256:" + "f" * 64

        with tempfile.TemporaryDirectory() as directory, \
                after_call(exports, "copy_regular_files", substitute) as copies:
            output = Path(directory) / "output"
            self.assertEqual(self.call(world, output), world.envelope)
            copies.assert_called_once()
            self.assertEqual(verify_build_export(output, plan=admitted), world.envelope)
        self.assertNotEqual(world.plan, admitted)

    def test_staged_bytes_changed_during_final_pair_read_are_reverified(self):
        world = merged_pair_world()
        name, stages = world.envelope["files"][0]["path"], []
        with tempfile.TemporaryDirectory() as directory, \
                after_call(exports, "copy_regular_files",
                           lambda root, stage_fd: stages.append(stage_of(root, stage_fd))), \
                at_final_read(world.api, lambda: tamper(stages[0] / name), downloads=3) as final:
            with self.assertRaisesRegex(MbError, "frozen export differs from its exact file inventory"):
                self.call(world, Path(directory) / "output")
            self.assertTrue(final["done"])
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
