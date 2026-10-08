"""Archive-derived copying and fixed-root admission; OS seams are explicit on Windows."""

import gzip
import hashlib
import io
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import python_installation as install
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.python_archive import inspect_python_installer
from mod_base.build_ci.worker import WorkerError
from mod_base.errors import MbError
from tests.test_ci_python_archive import tar_bytes


BOUNDARY = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
KIT = KitInstallation("a" * 40, "1.0.3", "sha256:" + "b" * 64, 3, 3, 1, 11)
VERSION = "3.11.17"
ROWS = [("./", "directory", None), ("./bin", "directory", None),
        ("./bin/python3.11", "file", b"known binary"), ("./bin/python3", "link", "python3.11"),
        ("./empty.py", "file", b""), ("./setup.sh", "file", b"must never execute"),
        ("./__pycache__", "directory", None), ("./__pycache__/module.pyc", "file", b"bytecode"),
        ("./site.pth", "file", b"import hostile")]


def fixture():
    raw = bytearray(tar_bytes(ROWS))
    # Independent synthetic approved fixture: executable binary source mode, header checksum.
    raw[1124:1132] = b"0000755\0"
    raw[1172:1180] = b"        "
    raw[1172:1180] = ("%06o\0 " % sum(raw[1024:1536])).encode()
    compressed = gzip.compress(raw, mtime=0)
    digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
    members = inspect_python_installer(io.BytesIO(compressed), expected_size=len(compressed), expected_digest=digest)
    return compressed, digest, members


def info(*, inode=99, size=0, mode=stat.S_IFDIR | 0o755, uid=0, gid=0, links=1):
    return SimpleNamespace(st_dev=1, st_ino=inode, st_size=size, st_mode=mode, st_uid=uid,
                           st_gid=gid, st_nlink=links, st_mtime_ns=1, st_ctime_ns=1)


class PythonInstallationTests(unittest.TestCase):
    def test_fixed_profile_hashes_equal_the_committed_publisher_lock(self):
        text = (Path(__file__).parents[1] / "requirements/python-ubuntu24-x64.sha256").read_text(encoding="ascii")
        records = dict((name, digest) for line in text.splitlines() if not line.startswith("#")
                       for digest, name in [line.split("  ")])
        self.assertEqual(records, {f"python-{version}-linux-24.04-x64.tar.gz": digest
                                  for version, (_, digest) in install._PROFILES.items()})

    def test_filter_is_source_only_and_manifest_is_derived_from_approved_bytes(self):
        _, digest, members = fixture()
        selected = install._selected(members, VERSION)
        self.assertEqual([item.path for item in selected], ["", "bin", "bin/python3", "bin/python3.11", "empty.py"])
        self.assertEqual(install._mode(selected[3]), 0o755)
        self.assertEqual(install._mode(selected[4]), 0o644)
        original = install._manifest(selected, VERSION, digest)
        self.assertNotEqual(original, install._manifest(selected, "3.12.15", digest))
        self.assertNotEqual(original, install._manifest(selected, VERSION, "sha256:" + "0" * 64))
        with self.assertRaises(WorkerError):
            install._selected(members, "3.12.15")
        from mod_base.build_ci.python_archive import PythonInstallerMember
        with self.assertRaises(WorkerError):
            install._selected(members + (PythonInstallerMember("alias", "link", 0o777, 0, None, "site.pth"),), VERSION)

    def _seams(self, stack, compressed):
        stack.enter_context(patch.object(install, "authenticate_privileged_host_boundary"))
        lock = stack.enter_context(patch.object(install, "_admit_lock"))
        stack.enter_context(patch.object(install, "_PROFILES", {VERSION: (len(compressed), hashlib.sha256(compressed).hexdigest())}))
        stack.enter_context(patch.object(install, "_open_directory", return_value=7))
        parent = stack.enter_context(patch.object(install, "_parent", return_value=8))
        initial = info(inode=66, size=len(compressed), mode=stat.S_IFREG | 0o600, uid=1001, gid=121)
        stack.enter_context(patch.object(install.os, "stat", return_value=initial))
        stack.enter_context(patch.object(install.os, "fstat", side_effect=lambda fd: initial if fd == 6 else info(inode=88 if fd == 8 else 99)))
        stack.enter_context(patch.object(install.os, "open", return_value=6))
        stack.enter_context(patch.object(install.os, "fdopen", return_value=io.BytesIO(compressed)))
        closed = stack.enter_context(patch.object(install.os, "close"))
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "fchown", "fchmod"):
            stack.enter_context(patch.object(install.os, name, 0 if name.startswith("O_") else None, create=True))
        # fchown/fchmod are callable OS seams, never executed in this Windows test.
        stack.enter_context(patch.object(install.os, "fchown", create=True))
        stack.enter_context(patch.object(install.os, "fchmod", create=True))
        copy = stack.enter_context(patch.object(install, "_copy"))
        verify = stack.enter_context(patch.object(install, "_verify"))
        def publish(path, writer):
            self.assertEqual(path.as_posix(), "/opt/hostedtoolcache/Python/3.11.17/x64")
            return writer(path.parent / ".x64.building-fixture", 9)
        atomic = stack.enter_context(patch.object(install, "atomic_directory", side_effect=publish))
        return initial, lock, parent, closed, copy, verify, atomic

    def test_fixed_exclusive_copy_is_bracketed_by_source_kit_and_parent_checks(self):
        compressed, digest, members = fixture()
        with ExitStack() as stack:
            initial, lock, parent, closed, copy, verify, atomic = self._seams(stack, compressed)
            result = install.install_privileged_python_archive(Path("/home/runner/archive"), boundary=BOUNDARY, installation=KIT, version=VERSION)
            self.assertEqual((result.version, result.archive_digest, result.device, result.inode), (VERSION, digest, 1, 99))
            self.assertEqual((result.entries, result.files, result.total_bytes), (5, 2, len(b"known binary")))
            self.assertEqual(copy.call_args.args[2], members)
            self.assertGreaterEqual(lock.call_count, 5)
            self.assertGreaterEqual(verify.call_count, 2)
            self.assertGreaterEqual(parent.call_count, 5)
            self.assertEqual(sorted(call.args[0] for call in closed.call_args_list[-3:]), [6, 7, 8])
            atomic.assert_called_once()

    def test_role_and_closed_version_precede_any_lock_or_file_access(self):
        with patch.object(install, "authenticate_privileged_host_boundary", side_effect=WorkerError("role")), \
                patch.object(install, "_admit_lock") as lock, self.assertRaises(WorkerError):
            install.install_privileged_python_archive(Path("/home/runner/archive"), boundary=BOUNDARY, installation=KIT, version=VERSION)
        lock.assert_not_called()
        for version, archive in [("3.11", Path("/home/runner/archive")), (True, Path("/home/runner/archive")), (VERSION, "not a path")]:
            with self.subTest(version=version), patch.object(install, "authenticate_privileged_host_boundary"), \
                    patch.object(install, "_admit_lock") as lock, self.assertRaises(WorkerError):
                install.install_privileged_python_archive(archive, boundary=BOUNDARY, installation=KIT, version=version)
            lock.assert_not_called()

    def test_bad_source_copy_hash_or_publication_never_returns_an_installation(self):
        compressed, _, _ = fixture()
        for case in ("owner", "hardlink", "size", "outside", "copy", "hash", "existing", "installed"):
            with self.subTest(case=case), ExitStack() as stack:
                initial, lock, parent, closed, copy, verify, atomic = self._seams(stack, compressed)
                if case == "owner": initial.st_uid = 2000
                if case == "hardlink": initial.st_nlink = 2
                if case == "size": initial.st_size += 1
                if case == "copy": copy.side_effect = WorkerError("copy drift")
                if case == "hash": stack.enter_context(patch.object(install, "_hash_archive", side_effect=WorkerError("hash drift")))
                if case == "existing": atomic.side_effect = WorkerError("existing output")
                if case == "installed": verify.side_effect = [None, WorkerError("installed bytes drift")]
                with self.assertRaises(MbError):
                    install.install_privileged_python_archive(Path("/outside/archive" if case == "outside" else "/home/runner/archive"),
                                       boundary=BOUNDARY, installation=KIT, version=VERSION)
                if case in ("owner", "hardlink", "size", "outside"):
                    copy.assert_not_called(); atomic.assert_not_called()

    def test_real_streaming_copy_preserves_empty_files_and_never_writes_excluded_content(self):
        compressed, _, members = fixture()
        selected = install._selected(members, VERSION)
        writes = {11: bytearray(), 12: bytearray()}
        def write(fd, data):
            count = min(2, len(data))
            writes[fd].extend(bytes(data[:count]))
            return count
        with ExitStack() as stack:
            stack.enter_context(patch.object(install, "_open_directory", return_value=7))
            new = stack.enter_context(patch.object(install, "_new_file", side_effect=[11, 12]))
            stack.enter_context(patch.object(install.os, "mkdir"))
            link = stack.enter_context(patch.object(install.os, "symlink"))
            stack.enter_context(patch.object(install.os, "write", side_effect=write))
            chmod = stack.enter_context(patch.object(install.os, "fchmod", create=True))
            stack.enter_context(patch.object(install.os, "fsync"))
            stack.enter_context(patch.object(install.os, "close"))
            verify = stack.enter_context(patch.object(install, "_verify"))
            install._copy(io.BytesIO(compressed), 9, members, selected, len(compressed))
            self.assertEqual([call.args[1] for call in new.call_args_list], ["bin/python3.11", "empty.py"])
            self.assertEqual(writes, {11: bytearray(b"known binary"), 12: bytearray()})
            link.assert_called_once_with("python3.11", "python3", dir_fd=7)
            self.assertIn(((11, 0o755), {}), [(call.args, call.kwargs) for call in chmod.call_args_list])
            self.assertIn(((12, 0o644), {}), [(call.args, call.kwargs) for call in chmod.call_args_list])
            verify.assert_called_once_with(9, selected)

    def test_zero_progress_write_and_changed_payload_reject_before_link_creation(self):
        compressed, _, members = fixture()
        selected = install._selected(members, VERSION)
        raw = gzip.decompress(compressed).replace(b"known binary", b"changed data")
        altered = gzip.compress(raw, mtime=0)
        for data, zero in ((compressed, True), (altered, False)):
            with self.subTest(zero=zero), ExitStack() as stack:
                stack.enter_context(patch.object(install, "_open_directory", return_value=7))
                stack.enter_context(patch.object(install, "_new_file", return_value=11))
                stack.enter_context(patch.object(install.os, "mkdir"))
                link = stack.enter_context(patch.object(install.os, "symlink"))
                stack.enter_context(patch.object(install.os, "write", side_effect=lambda fd, chunk: 0 if zero else len(chunk)))
                stack.enter_context(patch.object(install.os, "fchmod", create=True))
                stack.enter_context(patch.object(install.os, "fsync"))
                stack.enter_context(patch.object(install.os, "close"))
                with self.assertRaises(WorkerError):
                    install._copy(io.BytesIO(data), 9, members, selected, len(data))
                link.assert_not_called()

    def test_installed_lock_is_bounded_closed_and_stable(self):
        valid = (Path(__file__).parents[1] / "requirements/python-ubuntu24-x64.sha256").read_bytes()
        for raw in (valid, valid + valid, valid.replace(b"ae713815", b"00000000"), valid + b"\n", valid[:-1], valid.replace(b"\n", b"\r\n")):
            with self.subTest(raw=raw[:30]), ExitStack() as stack:
                stack.enter_context(patch.object(install, "authenticate_privileged_kit"))
                stack.enter_context(patch.object(install, "_open_directory", return_value=7))
                initial = info(size=len(raw), mode=stat.S_IFREG | 0o600)
                stack.enter_context(patch.object(install.os, "stat", return_value=initial))
                stack.enter_context(patch.object(install.os, "fstat", return_value=initial))
                stack.enter_context(patch.object(install.os, "open", return_value=6))
                stack.enter_context(patch.object(install.os, "read", side_effect=[raw, b""]))
                closed = stack.enter_context(patch.object(install.os, "close"))
                for flag in ("O_NOFOLLOW", "O_NONBLOCK"):
                    stack.enter_context(patch.object(install.os, flag, 0, create=True))
                if raw == valid:
                    install._admit_lock(KIT, BOUNDARY)
                else:
                    with self.assertRaises(WorkerError): install._admit_lock(KIT, BOUNDARY)
                self.assertEqual(sorted(call.args[0] for call in closed.call_args_list), [6, 7])

    def test_parent_rechecks_do_not_create_or_change_existing_directories(self):
        for uid, mode in ((0, 0o755), (1001, 0o755), (2000, 0o755), (0, 0o777)):
            with self.subTest(uid=uid, mode=mode), ExitStack() as stack:
                for flag in ("O_DIRECTORY", "O_NOFOLLOW"):
                    stack.enter_context(patch.object(install.os, flag, 0, create=True))
                stack.enter_context(patch.object(install.os, "open", return_value=6))
                stack.enter_context(patch.object(install, "_open_directory", return_value=7))
                stack.enter_context(patch.object(install.os, "fstat", return_value=info(uid=uid, mode=stat.S_IFDIR | mode)))
                stack.enter_context(patch.object(install.os, "close"))
                mkdir = stack.enter_context(patch.object(install.os, "mkdir"))
                chmod = stack.enter_context(patch.object(install.os, "fchmod", create=True))
                if uid in (0,1001) and mode == 0o755:
                    self.assertEqual(install._parent(VERSION, BOUNDARY, create=False), 7)
                else:
                    with self.assertRaises(WorkerError): install._parent(VERSION, BOUNDARY, create=False)
                mkdir.assert_not_called(); chmod.assert_not_called()
