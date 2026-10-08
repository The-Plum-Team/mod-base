"""Source-derived SDK admission; OS seams do not establish real hosted/root provenance."""

import io
import stat
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from mod_base.build_ci import python_setup as setup
from mod_base.build_ci.python_installation import PythonInstallation
from mod_base.build_ci.worker import WorkerError
from tests.test_ci_python_installation import BOUNDARY, KIT, VERSION, fixture, info


class PythonAuthenticationTests(unittest.TestCase):
    def fixture(self):
        data, digest, members = fixture()
        selected = setup._selected(members, VERSION)
        proof = PythonInstallation(VERSION, digest, setup._manifest(selected, VERSION, digest), len(selected),
                    sum(item.kind == "file" for item in selected), sum(item.size for item in selected), 1, 10)
        return data, proof, selected

    def seams(self, stack, data, proof):
        host = stack.enter_context(patch.object(setup, "authenticate_privileged_host_boundary"))
        lock = stack.enter_context(patch.object(setup, "_admit_lock"))
        stack.enter_context(patch.object(setup, "_PROFILES", {VERSION: (len(data), proof.archive_digest[7:])}))
        parents = stack.enter_context(patch.object(setup, "_parent", return_value=7))
        prefixes = stack.enter_context(patch.object(setup, "_installation_parent", return_value=9))
        directories = stack.enter_context(patch.object(setup, "_open_directory", side_effect=lambda parts, *, root: 8 if root == 7 else 10))
        cache = stack.enter_context(patch.object(setup, "_verify"))
        sdk = stack.enter_context(patch.object(setup, "_verify_installation"))
        stats = {7: info(inode=7, mode=stat.S_IFDIR | 0o700), 8: info(inode=8, mode=stat.S_IFDIR | 0o700),
                 9: info(inode=9), 10: info(inode=10),
                 6: info(inode=6, size=len(data), mode=stat.S_IFREG | 0o600)}
        stack.enter_context(patch.object(setup.os, "fstat", side_effect=lambda fd: stats[fd]))
        named = stack.enter_context(patch.object(setup.os, "stat", side_effect=lambda name, *, dir_fd, follow_symlinks:
            stats[6 if name == "installer.tar.gz" else 8 if name == VERSION else 10]))
        opened = stack.enter_context(patch.object(setup.os, "open", return_value=6))
        stream = stack.enter_context(patch.object(setup.os, "fdopen", side_effect=lambda *a, **kw: io.BytesIO(data)))
        close = stack.enter_context(patch.object(setup.os, "close"))
        for name in ("O_NOFOLLOW", "O_NONBLOCK"):
            stack.enter_context(patch.object(setup.os, name, 0, create=True))
        return host, lock, parents, prefixes, cache, sdk, stats, named, opened, stream, close

    def test_rederive_all_counts_and_manifest_before_read_only_sdk_check(self):
        data, proof, selected = self.fixture()
        with ExitStack() as stack:
            host, lock, parents, prefixes, cache, sdk, _, _, _, _, close = self.seams(stack, data, proof)
            result = setup.authenticate_privileged_python_installation(proof, boundary=BOUNDARY, installation=KIT)
            self.assertEqual("/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11", result)
            sdk.assert_called_once_with(10, selected)
            self.assertEqual(2, cache.call_count)
            self.assertEqual((3, 3), (host.call_count, lock.call_count))
            self.assertTrue(all(call.kwargs == {"create": False} for call in parents.call_args_list))
            self.assertTrue(all(call.kwargs == {"create": False} for call in prefixes.call_args_list))
            self.assertTrue({6, 7, 8, 9, 10}.issubset({call.args[0] for call in close.call_args_list}))

    def test_forged_receipt_fields_never_authorize_installed_tree(self):
        data, proof, _ = self.fixture()
        for field, value in (("version", "3.12.10"), ("archive_digest", "sha256:" + "f" * 64),
                             ("manifest_digest", "sha256:" + "e" * 64), ("entries", proof.entries + 1),
                             ("files", proof.files + 1), ("total_bytes", proof.total_bytes + 1),
                             ("device", 2), ("inode", 11), ("entries", True), ("inode", -1)):
            with self.subTest(field=field, value=value), ExitStack() as stack:
                *_, sdk, stats, named, opened, stream, close = self.seams(stack, data, proof)
                with self.assertRaises(WorkerError):
                    setup.authenticate_privileged_python_installation(replace(proof, **{field: value}), boundary=BOUNDARY, installation=KIT)
                sdk.assert_not_called()

    def test_source_sdk_lock_role_and_binding_failures_return_no_path(self):
        data, proof, _ = self.fixture()
        for kind in ("role", "lock", "cache", "sdk", "source-bytes", "named-source", "named-sdk", "parent", "source-owner"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, lock, parents, prefixes, cache, sdk, stats, named, opened, stream, close = self.seams(stack, data, proof)
                if kind in ("role", "lock", "cache", "sdk"):
                    {"role": host, "lock": lock, "cache": cache, "sdk": sdk}[kind].side_effect = WorkerError("admission failed")
                elif kind == "source-bytes":
                    stream.side_effect = lambda *a, **kw: io.BytesIO(data[:-1] + bytes([data[-1] ^ 1]))
                elif kind == "source-owner":
                    stats[6] = info(inode=6, size=len(data), mode=stat.S_IFREG | 0o600, uid=1001)
                elif kind == "parent":
                    parents.side_effect = [7, 8]
                else:
                    original = named.side_effect
                    count = [0]
                    def drift(name, *, dir_fd, follow_symlinks):
                        count[0] += 1
                        result = original(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                        if kind == "named-source" and name == "installer.tar.gz" and count[0] > 1:
                            return info(inode=66, size=len(data), mode=stat.S_IFREG | 0o600)
                        if kind == "named-sdk" and name == "x64":
                            return info(inode=100)
                        return result
                    named.side_effect = drift
                with self.assertRaises(WorkerError):
                    setup.authenticate_privileged_python_installation(proof, boundary=BOUNDARY, installation=KIT)
                if kind in ("role", "lock"):
                    opened.assert_not_called()

    def test_changed_cache_after_sdk_scan_is_rejected(self):
        data, proof, _ = self.fixture()
        with ExitStack() as stack:
            _, _, _, _, _, sdk, stats, _, _, _, _ = self.seams(stack, data, proof)
            def mutate(*args):
                stats[8] = info(inode=88, mode=stat.S_IFDIR | 0o700)
            sdk.side_effect = mutate
            with self.assertRaises(WorkerError):
                setup.authenticate_privileged_python_installation(proof, boundary=BOUNDARY, installation=KIT)
