"""Closed root-record data/publication; OS seams never claim Linux origin on Windows."""

import copy
import unittest
import stat
from types import SimpleNamespace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import installation_record as record
from mod_base.build_ci import handoff
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerError
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_kit_installation


BOUNDARY = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
INSTALLATION = KitInstallation("a" * 40, "1.0.3", "sha256:" + "b" * 64, 3, 3, 1, 11)


class KitInstallationRecordTests(unittest.TestCase):
    def test_closed_new_kind_widths_types_fields_versions_and_byte_cap(self):
        document = ci_kit_installation()
        self.assertEqual(load_document(canonical_json(document), kind=document["kind"]), document)
        for mutate in (lambda d: d.update(program="unsafe"), lambda d: d.update(path="/tmp/candidate"),
                       lambda d: d.update(inode=0), lambda d: d.update(device=True),
                       lambda d: d.update(files=1.0), lambda d: d.update(total_bytes=-1),
                       lambda d: d.update(inode=limits.MAX_CI_FILE_ID+1),
                       lambda d: d.update(files=limits.MAX_CI_KIT_INSTALL_FILES+1),
                       lambda d: d.update(schema_version=2), lambda d: d.pop("tree_digest"),
                       lambda d: d["kit"].update(repository="other/repo")):
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(MbError):
                load_document(canonical_json(changed), kind=document["kind"])
        with self.assertRaises(MbError):
            load_document(b" " * (limits.MAX_CI_KIT_INSTALL_RECORD_BYTES+1), kind=document["kind"])

    def test_writer_fixed_private_bytes_and_copy_reauthentication(self):
        raw = canonical_json(ci_kit_installation())
        with ExitStack() as stack:
            guard = stack.enter_context(patch.object(record, "authenticate_privileged_host_boundary"))
            authenticate = stack.enter_context(patch.object(record, "authenticate_privileged_kit"))
            stack.enter_context(patch.object(record, "_layout"))
            publish = stack.enter_context(patch.object(record, "atomic_directory",
                side_effect=lambda path, fill: fill(Path("/private-stage"), 31)))
            write = stack.enter_context(patch.object(record, "write_new"))
            stack.enter_context(patch.object(record.os, "O_NOFOLLOW", 0, create=True))
            stack.enter_context(patch.object(record.os, "open", return_value=17))
            chmod = stack.enter_context(patch.object(record.os, "fchmod", create=True))
            stack.enter_context(patch.object(record.os, "fsync"))
            stack.enter_context(patch.object(record.os, "close"))
            metadata = stack.enter_context(patch.object(record, "authenticate_tree_private_access"))
            stack.enter_context(patch.object(record, "read_child_file", return_value=raw))
            result = record.record_privileged_kit_installation(INSTALLATION, boundary=BOUNDARY)
        self.assertEqual(result, ci_kit_installation())
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], Path(str(record.PRIVILEGED_KIT_RECORD_ROOT)))
        write.assert_called_once_with(31, grammar.CI_KIT_INSTALLATION_NAME, raw)
        chmod.assert_called_once_with(17, 0o600)
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual(metadata.call_count, 2)
        self.assertEqual(guard.call_count, 2)
        for call in metadata.call_args_list:
            self.assertEqual(call.kwargs, dict(owner_uid=0, owner_gid=0, max_entries=1))

    def _read(self, raw, *, final=None, copy_error=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(record, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(record, "_layout"))
            stack.enter_context(patch.object(record, "authenticate_tree_private_access"))
            reads = stack.enter_context(patch.object(record, "_read_private_record", side_effect=[raw, raw if final is None else final]))
            authenticate = stack.enter_context(patch.object(record, "authenticate_privileged_kit", side_effect=copy_error))
            result = record.read_privileged_kit_installation(boundary=BOUNDARY)
            self.assertEqual(authenticate.call_args.args[0], INSTALLATION)
            for call in reads.call_args_list:
                self.assertEqual(call.args, (Path(str(record.PRIVILEGED_KIT_RECORD_ROOT)),))
                self.assertEqual(call.kwargs, dict(name=grammar.CI_KIT_INSTALLATION_NAME, owner_uid=0, owner_gid=0,
                    max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES, label="kit installation record"))
            return result

    def test_reader_reconstructs_only_strict_canonical_data_and_rechecks_copy(self):
        raw = canonical_json(ci_kit_installation())
        self.assertEqual(self._read(raw), INSTALLATION)
        for malformed in (b" " + raw, raw.replace(b'"inode":11', b'"inode":11,"inode":11'),
                          raw.replace(b'"files":3', b'"files":NaN'), b"{", b"\xff"):
            with self.subTest(raw=malformed), self.assertRaises(MbError):
                self._read(malformed)
        with self.assertRaisesRegex(MbError, "changed during"):
            self._read(raw, final=b"different")
        with self.assertRaisesRegex(MbError, "copy changed"):
            self._read(raw, copy_error=WorkerError("copy changed"))

    def test_role_failures_precede_any_publication_or_record_read(self):
        for operation in (lambda: record.record_privileged_kit_installation(None, boundary=BOUNDARY),
                          lambda: record.read_privileged_kit_installation(boundary=BOUNDARY)):
            with patch.object(record, "authenticate_privileged_host_boundary", side_effect=WorkerError("not root")), \
                    patch.object(record, "authenticate_privileged_kit") as authenticate, \
                    patch.object(record, "atomic_directory") as publish, \
                    patch.object(record, "_read_private_record") as read, self.assertRaises(MbError):
                operation()
            authenticate.assert_not_called()
            publish.assert_not_called()
            read.assert_not_called()

    def test_shared_descriptor_reader_enforces_root_ownership_single_leaf_and_cap(self):
        raw = canonical_json(ci_kit_installation())
        root = SimpleNamespace(st_dev=1, st_ino=10, st_mode=stat.S_IFDIR | 0o700,
                               st_uid=0, st_gid=0, st_nlink=2, st_size=512, st_mtime_ns=1, st_ctime_ns=1)
        leaf = SimpleNamespace(**{**vars(root), "st_ino": 11, "st_mode": stat.S_IFREG | 0o600,
                                   "st_nlink": 1, "st_size": len(raw)})
        class Listing:
            def __init__(self, names): self.names = names
            def __enter__(self): return iter(SimpleNamespace(name=name) for name in self.names)
            def __exit__(self, *args): return False
        for change in (None, "owner", "mode", "link", "oversize", "extra", "empty", "changed"):
            current = SimpleNamespace(**vars(leaf))
            names = [grammar.CI_KIT_INSTALLATION_NAME]
            if change == "owner": current.st_uid = BOUNDARY.uid
            elif change == "mode": current.st_mode = stat.S_IFREG | 0o640
            elif change == "link": current.st_nlink = 2
            elif change == "oversize": current.st_size = limits.MAX_CI_KIT_INSTALL_RECORD_BYTES+1
            elif change == "extra": names.append("unexpected")
            elif change == "empty": names = []
            reads = iter((raw, b""))
            consumed = False
            def read(fd, cap):
                nonlocal consumed
                self.assertLessEqual(cap, limits.MAX_CI_KIT_INSTALL_RECORD_BYTES+1)
                consumed = True
                return next(reads)
            def fstat(fd):
                if fd != 52: return root
                if change == "changed" and consumed:
                    return SimpleNamespace(**{**vars(current), "st_ino": current.st_ino+1})
                return current
            with self.subTest(change=change), ExitStack() as stack:
                stack.enter_context(patch.object(handoff, "_open_directory", side_effect=(51, 53)))
                stack.enter_context(patch.object(handoff.os, "O_NOFOLLOW", 0, create=True))
                stack.enter_context(patch.object(handoff.os, "O_NONBLOCK", 0, create=True))
                stack.enter_context(patch.object(handoff.os, "fstat", side_effect=fstat))
                stack.enter_context(patch.object(handoff.os, "scandir", return_value=Listing(names)))
                stack.enter_context(patch.object(handoff.os, "stat", return_value=current))
                stack.enter_context(patch.object(handoff.os, "open", return_value=52))
                stack.enter_context(patch.object(handoff.os, "read", side_effect=read))
                close = stack.enter_context(patch.object(handoff.os, "close"))
                if change: stack.enter_context(self.assertRaises(MbError))
                value = handoff._read_private_record(Path(str(record.PRIVILEGED_KIT_RECORD_ROOT)),
                    name=grammar.CI_KIT_INSTALLATION_NAME, owner_uid=0, owner_gid=0,
                    max_bytes=limits.MAX_CI_KIT_INSTALL_RECORD_BYTES, label="kit installation record")
                if not change: self.assertEqual(value, raw)
            self.assertIn(51, [call.args[0] for call in close.call_args_list])

    def test_io_failures_normalize_and_never_publish_on_failed_copy_admission(self):
        with patch.object(record, "authenticate_privileged_host_boundary"), \
                patch.object(record, "authenticate_privileged_kit", side_effect=WorkerError("bad copy")), \
                patch.object(record, "atomic_directory") as publish, self.assertRaises(MbError):
            record.record_privileged_kit_installation(INSTALLATION, boundary=BOUNDARY)
        publish.assert_not_called()
        for operation in (lambda: record.record_privileged_kit_installation(INSTALLATION, boundary=BOUNDARY),
                          lambda: record.read_privileged_kit_installation(boundary=BOUNDARY)):
            with patch.object(record, "authenticate_privileged_host_boundary", side_effect=OSError("I/O")), \
                    self.assertRaises(WorkerError):
                operation()
