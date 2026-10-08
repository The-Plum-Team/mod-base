"""Root cache orchestration with explicit OS seams; real bytes exercise write/read helpers."""

import hashlib
import io
import stat
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import python_setup as setup
from mod_base.build_ci.worker import WorkerError
from mod_base.errors import MbError
from tests.test_ci_python_installation import BOUNDARY, KIT, VERSION, info


DATA = b"independently authored fixture bytes"
DIGEST = "sha256:" + hashlib.sha256(DATA).hexdigest()


class PythonSetupTests(unittest.TestCase):
    def seams(self, stack):
        host = stack.enter_context(patch.object(setup, "authenticate_privileged_host_boundary"))
        lock = stack.enter_context(patch.object(setup, "_admit_lock"))
        download = stack.enter_context(patch.object(setup, "download_python_installer", return_value=DATA))
        publisher = stack.enter_context(patch.object(setup, "_authenticate"))
        stack.enter_context(patch.object(setup, "_PROFILES", {VERSION: (len(DATA), DIGEST[7:])}))
        parent = stack.enter_context(patch.object(setup, "_parent", return_value=7))
        stack.enter_context(patch.object(setup, "_open_directory", return_value=9))
        stack.enter_context(patch.object(setup.os, "fstat", side_effect=lambda fd: info(inode=fd, mode=stat.S_IFDIR | 0o700)))
        close = stack.enter_context(patch.object(setup.os, "close"))
        for name in ("fchown", "fchmod"):
            stack.enter_context(patch.object(setup.os, name, create=True))
        write = stack.enter_context(patch.object(setup, "_write"))
        verify = stack.enter_context(patch.object(setup, "_verify"))

        def publish(path, writer):
            self.assertEqual(path.as_posix(), "/home/runner/.mod-base-python/3.11.17")
            return writer(path.parent / ".3.11.17.building-fixture", 9)

        atomic = stack.enter_context(patch.object(setup, "atomic_directory", side_effect=publish))
        return host, lock, download, publisher, parent, write, verify, atomic, close

    def test_fixed_exclusive_publication_and_original_metadata_rechecks(self):
        with ExitStack() as stack:
            host, lock, download, publisher, parent, write, verify, atomic, close = self.seams(stack)
            api = object()
            output = setup.cache_privileged_python_installer(api, boundary=BOUNDARY, installation=KIT, version=VERSION)
            self.assertEqual(output.as_posix(), "/home/runner/.mod-base-python/3.11.17/installer.tar.gz")
            download.assert_called_once_with(api, version=VERSION)
            write.assert_called_once_with(9, DATA)
            self.assertEqual(3, verify.call_count)
            self.assertEqual(2, publisher.call_count)
            self.assertGreaterEqual(host.call_count, 6)
            self.assertEqual(host.call_count, lock.call_count)
            self.assertEqual([True] + [False] * (parent.call_count - 1), [call.kwargs["create"] for call in parent.call_args_list])
            self.assertEqual(1, atomic.call_count)
            self.assertIn(7, [call.args[0] for call in close.call_args_list])

    def test_role_version_and_lock_reject_before_network_or_files(self):
        with ExitStack() as stack:
            host, lock, download, _, parent, _, _, atomic, _ = self.seams(stack)
            host.side_effect = WorkerError("wrong role")
            with self.assertRaises(WorkerError):
                setup.cache_privileged_python_installer(object(), boundary=BOUNDARY, installation=KIT, version=VERSION)
            host.side_effect = None
            for version in (True, None, "3.12.10", "../escape"):
                with self.subTest(version=version), self.assertRaises(WorkerError):
                    setup.cache_privileged_python_installer(object(), boundary=BOUNDARY, installation=KIT, version=version)
            lock.side_effect = WorkerError("wrong kit lock")
            with self.assertRaises(WorkerError):
                setup.cache_privileged_python_installer(object(), boundary=BOUNDARY, installation=KIT, version=VERSION)
            download.assert_not_called()
            parent.assert_not_called()
            atomic.assert_not_called()

    def test_cache_parent_rechecks_never_adopt_foreign_metadata(self):
        original = info(inode=7, mode=stat.S_IFDIR | 0o700)
        with ExitStack() as stack:
            for name in ("O_DIRECTORY", "O_NOFOLLOW"):
                stack.enter_context(patch.object(setup.os, name, 0, create=True))
            stack.enter_context(patch.object(setup, "_open_directory", return_value=5))
            named = stack.enter_context(patch.object(setup.os, "stat", return_value=original))
            stack.enter_context(patch.object(setup.os, "open", return_value=7))
            opened = stack.enter_context(patch.object(setup.os, "fstat", return_value=original))
            mkdir = stack.enter_context(patch.object(setup.os, "mkdir"))
            stack.enter_context(patch.object(setup.os, "close"))
            self.assertEqual(7, setup._parent(create=False))
            mkdir.assert_not_called()
            for changes in ({"uid": 1001}, {"gid": 121}, {"mode": stat.S_IFDIR | 0o755},
                            {"mode": stat.S_IFLNK | 0o700}):
                with self.subTest(changes=changes):
                    named.return_value = info(inode=7, **changes)
                    with self.assertRaises(WorkerError):
                        setup._parent(create=False)
            named.return_value = original
            opened.return_value = info(inode=77, mode=stat.S_IFDIR | 0o700)
            with self.assertRaises(WorkerError):
                setup._parent(create=False)
            opened.return_value = original
            named.side_effect = FileNotFoundError()
            with self.assertRaises(FileNotFoundError):
                setup._parent(create=False)
            mkdir.assert_not_called()
            named.side_effect = [FileNotFoundError(), original]
            self.assertEqual(7, setup._parent(create=True))
            mkdir.assert_called_once_with(".mod-base-python", 0o700, dir_fd=5)

    def test_changed_parents_roots_bytes_metadata_and_existing_output_reject(self):
        for kind in ("parent", "published", "write", "verify", "publisher", "late-publisher", "existing", "host-after-download"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, _, _, publisher, _, write, verify, atomic, _ = self.seams(stack)
                if kind == "parent":
                    stack.enter_context(patch.object(setup.os, "fstat", side_effect=lambda fd: info(inode=fd + (10 if fd == 7 else 0), mode=stat.S_IFDIR | 0o700)))
                    # Initial retained parent is 17; the next independently reopened one is 18.
                    stack.enter_context(patch.object(setup, "_parent", side_effect=[7, 8]))
                elif kind == "published":
                    stack.enter_context(patch.object(setup, "_open_directory", return_value=10))
                elif kind in ("write", "verify"):
                    (write if kind == "write" else verify).side_effect = WorkerError("bad physical bytes")
                elif kind in ("publisher", "late-publisher"):
                    publisher.side_effect = WorkerError("producer drift") if kind == "publisher" else [None, WorkerError("late producer drift")]
                elif kind == "existing":
                    atomic.side_effect = MbError("existing output")
                else:
                    host.side_effect = [None, WorkerError("changed host")]
                with self.assertRaises(MbError):
                    setup.cache_privileged_python_installer(object(), boundary=BOUNDARY, installation=KIT, version=VERSION)

    def test_source_only_setup_preserves_exact_cache_path_and_rechecks_after_install(self):
        archive = Path("/home/runner/.mod-base-python/3.11.17/installer.tar.gz")
        proof = object()
        api = object()
        with patch.object(setup, "cache_privileged_python_installer", return_value=archive) as cache, \
             patch.object(setup, "install_privileged_python_archive", return_value=proof) as install, \
             patch.object(setup, "_authenticate") as publisher, \
             patch.object(setup, "authenticate_privileged_python_installation") as authenticate:
            self.assertIs(proof, setup.install_privileged_python_from_publisher(api, boundary=BOUNDARY, installation=KIT, version=VERSION))
            cache.assert_called_once_with(api, boundary=BOUNDARY, installation=KIT, version=VERSION)
            install.assert_called_once_with(archive, boundary=BOUNDARY, installation=KIT, version=VERSION)
            publisher.assert_called_once_with(api, VERSION)
            authenticate.assert_called_once_with(proof, boundary=BOUNDARY, installation=KIT)
            publisher.side_effect = WorkerError("changed producer after installation")
            with self.assertRaises(WorkerError):
                setup.install_privileged_python_from_publisher(api, boundary=BOUNDARY, installation=KIT, version=VERSION)

    def test_partial_write_preserves_all_bytes_and_zero_progress_rejects(self):
        actual = bytearray()
        def write(fd, chunk):
            self.assertEqual(6, fd)
            actual.extend(chunk[:3])
            return min(3, len(chunk))
        with ExitStack() as stack:
            for name in ("O_NOFOLLOW", "fchown", "fchmod"):
                stack.enter_context(patch.object(setup.os, name, 0 if name.startswith("O_") else None, create=True))
            stack.enter_context(patch.object(setup.os, "fchown", create=True))
            stack.enter_context(patch.object(setup.os, "fchmod", create=True))
            opened = stack.enter_context(patch.object(setup.os, "open", return_value=6))
            closed = stack.enter_context(patch.object(setup.os, "close"))
            stack.enter_context(patch.object(setup.os, "fsync"))
            stack.enter_context(patch.object(setup.os, "write", side_effect=write))
            setup._write(9, DATA)
            self.assertEqual(DATA, bytes(actual))
            self.assertEqual(("installer.tar.gz", 9, 0o600), (opened.call_args.args[0], opened.call_args.kwargs["dir_fd"], opened.call_args.args[2]))
            with patch.object(setup.os, "write", return_value=0), self.assertRaises(WorkerError):
                setup._write(9, DATA)
            self.assertEqual(2, closed.call_count)

    def test_real_hash_reader_rejects_inventory_metadata_content_and_mutation(self):
        directory = info(inode=9, mode=stat.S_IFDIR | 0o700)
        file = info(inode=6, mode=stat.S_IFREG | 0o600, size=len(DATA))
        with ExitStack() as stack:
            for name in ("O_NOFOLLOW", "O_NONBLOCK"):
                stack.enter_context(patch.object(setup.os, name, 0, create=True))
            listing = SimpleNamespace(names=["installer.tar.gz"])
            stack.enter_context(patch.object(setup.os, "scandir", side_effect=lambda fd: nullcontext(
                iter(SimpleNamespace(name=name) for name in listing.names))))
            named = stack.enter_context(patch.object(setup.os, "stat", return_value=file))
            opened = stack.enter_context(patch.object(setup.os, "fstat", side_effect=lambda fd: file if fd == 6 else directory))
            stack.enter_context(patch.object(setup.os, "open", return_value=6))
            stream = stack.enter_context(patch.object(setup.os, "fdopen", side_effect=lambda *a, **k: io.BytesIO(DATA)))
            stack.enter_context(patch.object(setup.os, "close"))
            setup._verify(9, len(DATA), DIGEST)
            for changes in ({"links": 2}, {"uid": 1001}, {"gid": 121}, {"mode": stat.S_IFREG | 0o644},
                            {"mode": stat.S_IFLNK | 0o600}, {"size": len(DATA) + 1}):
                with self.subTest(changes=changes):
                    named.return_value = info(inode=6, mode=stat.S_IFREG | 0o600, size=len(DATA), **{key: value for key, value in changes.items() if key not in ("mode", "size")})
                    for key in ("mode", "size"):
                        if key in changes:
                            setattr(named.return_value, "st_" + key, changes[key])
                    with self.assertRaises(WorkerError):
                        setup._verify(9, len(DATA), DIGEST)
            named.return_value = file
            listing.names = ["installer.tar.gz", "extra"]
            with self.assertRaises(WorkerError):
                setup._verify(9, len(DATA), DIGEST)
            listing.names = ["installer.tar.gz"]
            stream.side_effect = lambda *a, **k: io.BytesIO(DATA[:-1] + b"!")
            with self.assertRaises(WorkerError):
                setup._verify(9, len(DATA), DIGEST)
            stream.side_effect = lambda *a, **k: io.BytesIO(DATA)
            named.side_effect = [file, info(inode=66, mode=stat.S_IFREG | 0o600, size=len(DATA))]
            with self.assertRaises(WorkerError):
                setup._verify(9, len(DATA), DIGEST)

    def test_inventory_scan_stops_at_the_second_entry(self):
        consumed = []
        def entries():
            for name in ("installer.tar.gz", "extra", "must not be read"):
                consumed.append(name)
                yield SimpleNamespace(name=name)
        with patch.object(setup.os, "fstat", return_value=info(mode=stat.S_IFDIR | 0o700)), \
             patch.object(setup.os, "scandir", return_value=nullcontext(entries())), \
             patch.object(setup.os, "open") as opened:
            with self.assertRaises(WorkerError):
                setup._verify(9, len(DATA), DIGEST)
            self.assertEqual(["installer.tar.gz", "extra"], consumed)
            opened.assert_not_called()
