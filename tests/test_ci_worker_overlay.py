"""Candidate-only overlay copying; explicit seams establish no physical/root activation."""

import stat
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import worker_overlay as overlay
from mod_base.build_ci.worker import WorkerError
from mod_base.pin import Pin
from tests.test_ci_gradle_cache import ACCOUNT
from tests.test_ci_python_installation import BOUNDARY, info


PIN = Pin("a" * 40, "v1.0.3", ())
DIGEST = "sha256:" + "b" * 64
SOURCE = Path("/home/runner/seed-overlay")
RECORDS = [{"path": "src/mod_base/__init__.py", "size": 0, "sha256": "a" * 64}]


class WorkerOverlayTests(unittest.TestCase):
    def seams(self, stack):
        host = stack.enter_context(patch.object(overlay, "authenticate_privileged_host_boundary"))
        account = stack.enter_context(patch.object(overlay, "authenticate_worker_account", return_value=ACCOUNT))
        quiet = stack.enter_context(patch.object(overlay, "_quiet"))
        terminate = stack.enter_context(patch.object(overlay, "terminate_worker"))
        release = stack.enter_context(patch.object(overlay, "verify_released"))
        admit = stack.enter_context(patch.object(overlay, "_admit", return_value=RECORDS))
        copied = stack.enter_context(patch.object(overlay, "copy_regular_data_files", return_value=RECORDS))
        grant = stack.enter_context(patch.object(overlay, "grant_regular_data_read_access", return_value=RECORDS))
        plain = stack.enter_context(patch.object(overlay, "_plain"))
        def directory(parts, *, root=None):
            return 7 if parts[-1] == "seed-overlay" else 8 if parts[-1] == "repository" else 9 if parts[-1] == "out" else 10
        stack.enter_context(patch.object(overlay, "_open_directory", side_effect=directory))
        stats = {7: info(inode=7, uid=BOUNDARY.uid), 8: info(inode=8, uid=BOUNDARY.uid),
                 9: info(inode=9), 10: info(inode=10, uid=ACCOUNT.uid, gid=ACCOUNT.gid)}
        stack.enter_context(patch.object(overlay.os, "fstat", side_effect=lambda fd: stats[fd]))
        stack.enter_context(patch.object(overlay.os, "close"))
        for name in ("fchown", "fchmod", "mkdir"):
            stack.enter_context(patch.object(overlay.os, name, create=True))
        def publish(path, writer):
            self.assertEqual(path.as_posix(), str(overlay.WORKER_ROOT / "repository/out/mod-base-kit"))
            return writer(path.parent / ".mod-base-kit.building-fixture", 10)
        atomic = stack.enter_context(patch.object(overlay, "atomic_directory", side_effect=publish))
        return host, account, quiet, terminate, release, admit, copied, grant, plain, atomic, stats

    def test_exclusive_candidate_copy_preserves_empty_files_and_rechecks_pin(self):
        with ExitStack() as stack:
            host, account, quiet, terminate, release, admit, copied, grant, plain, atomic, stats = self.seams(stack)
            api = object()
            self.assertEqual(RECORDS, overlay.stage_privileged_worker_overlay(api, SOURCE, boundary=BOUNDARY,
                account=ACCOUNT, pin=PIN, expected_digest=DIGEST))
            self.assertEqual(3, release.call_count)
            copied.assert_called_once_with(SOURCE, 10, **overlay._BOUNDS)
            grant.assert_called_once()
            plain.assert_called_once_with(10, ACCOUNT)
            self.assertEqual(1, atomic.call_count)
            terminate.assert_not_called()

    def test_release_source_copy_and_handoff_failures_lock_the_worker(self):
        for kind in ("release", "source", "copy", "handoff", "plain", "late-release", "binding"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, terminate, release, admit, copied, grant, plain, atomic, stats = self.seams(stack)
                if kind in ("release", "source", "plain"):
                    {"release": release, "source": admit, "plain": plain}[kind].side_effect = WorkerError("admission")
                elif kind == "copy":
                    copied.return_value = []
                elif kind == "handoff":
                    grant.return_value = []
                elif kind == "late-release":
                    release.side_effect = [None, None, WorkerError("pin drift")]
                else:
                    def mutate(*args, **kwargs):
                        stats[7] = info(inode=70, uid=BOUNDARY.uid)
                        return RECORDS
                    copied.side_effect = mutate
                with self.assertRaises(WorkerError):
                    overlay.stage_privileged_worker_overlay(object(), SOURCE, boundary=BOUNDARY,
                        account=ACCOUNT, pin=PIN, expected_digest=DIGEST)
                terminate.assert_called_once_with(ACCOUNT)

    def test_invalid_role_pin_digest_or_source_never_copies(self):
        for kind in ("role", "pin", "digest", "outside", "account"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, terminate, release, admit, copied, grant, plain, atomic, stats = self.seams(stack)
                pin, digest, source, candidate = PIN, DIGEST, SOURCE, ACCOUNT
                if kind == "role": host.side_effect = WorkerError("role")
                elif kind == "pin": pin = Pin(PIN.sha, "v../escape", ())
                elif kind == "digest": digest = "unknown"
                elif kind == "outside": source = Path("/tmp/unprotected")
                else: candidate = object()
                with self.assertRaises(Exception):
                    overlay.stage_privileged_worker_overlay(object(), source, boundary=BOUNDARY,
                        account=candidate, pin=pin, expected_digest=digest)
                copied.assert_not_called()

    def test_admission_binds_stamp_digest_locks_and_non_executable_source(self):
        names = [*overlay.DIGESTED_DIRS, overlay.STAMP_NAME]
        with ExitStack() as stack:
            stack.enter_context(patch.object(overlay, "_paths", return_value=((RECORDS[0]["path"],), 4)))
            stack.enter_context(patch.object(overlay, "_open_directory", return_value=7))
            stack.enter_context(patch.object(overlay.os, "close"))
            stack.enter_context(patch.object(overlay.os, "scandir", side_effect=lambda fd: nullcontext(iter(SimpleNamespace(name=name) for name in names))))
            stack.enter_context(patch.object(overlay, "validate_tree_entries"))
            records = stack.enter_context(patch.object(overlay, "regular_data_records", return_value=RECORDS))
            stamp = stack.enter_context(patch.object(overlay, "read_stamp", return_value=overlay.stamp_document(PIN, DIGEST)))
            digest = stack.enter_context(patch.object(overlay, "kit_tree_digest", return_value=DIGEST))
            locks = stack.enter_context(patch.object(overlay, "verify_staged_files"))
            self.assertEqual(RECORDS, overlay._admit(SOURCE, PIN, DIGEST))
            locks.assert_called_once_with(SOURCE)
            for kind in ("stamp", "digest", "extra-root"):
                with self.subTest(kind=kind):
                    stamp.return_value = {} if kind == "stamp" else overlay.stamp_document(PIN, DIGEST)
                    digest.return_value = "sha256:" + "c" * 64 if kind == "digest" else DIGEST
                    if kind == "extra-root": names.append("credentials")
                    with self.assertRaises(WorkerError): overlay._admit(SOURCE, PIN, DIGEST)
