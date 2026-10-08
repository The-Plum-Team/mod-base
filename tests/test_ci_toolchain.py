"""Read-only closure admission; actual Linux permission tests remain mandatory."""

import stat
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import toolchain
from mod_base.build_ci.worker import WorkerAccount, WorkerError
from mod_base.build_ci.host import HostBoundary
from mod_base.errors import MbError
from mod_base.model import limits


ROOT = "/opt/hostedtoolcache/Python/x64"
BOUNDARY = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)


class Filesystem:
    def __init__(self):
        self.nodes = {}
        self.descriptors = {}
        self.next_fd = 10
        for name in ("/", "/opt", "/opt/hostedtoolcache", "/opt/hostedtoolcache/Python", ROOT,
                     ROOT + "/bin", ROOT + "/lib"):
            self.add(name, stat.S_IFDIR | 0o755)
        self.add(ROOT + "/bin/python3.11", stat.S_IFREG | 0o755, size=3)
        self.add(ROOT + "/bin/python3", stat.S_IFLNK | 0o777, target="python3.11")
        self.add(ROOT + "/lib/site.py", stat.S_IFREG | 0o644, size=5)

    def add(self, name, mode, *, owner=1001, size=0, target=None):
        self.nodes[name] = SimpleNamespace(st_dev=1, st_ino=len(self.nodes) + 1, st_mode=mode,
                                          st_uid=owner, st_gid=121, st_nlink=1,
                                          st_size=len(target if isinstance(target, bytes) else target.encode()) if target is not None else size,
                                          st_mtime_ns=1, st_ctime_ns=1, target=target)

    def path(self, name, directory):
        if isinstance(name, bytes):
            name = name.decode()
        return name if name.startswith("/") else self.descriptors[directory].rstrip("/") + "/" + name

    def opening(self, name, flags, *, dir_fd=None):
        path = self.path(name, dir_fd)
        if not stat.S_ISDIR(self.nodes[path].st_mode):
            raise OSError("fake no-follow directory rejection")
        descriptor = self.next_fd
        self.next_fd += 1
        self.descriptors[descriptor] = path
        return descriptor

    def info(self, name, *, dir_fd=None, follow_symlinks=False):
        return self.nodes[self.path(name, dir_fd)]

    def inspect(self, roots=(ROOT,), operation=None):
        class Listing:
            def __init__(self, entries):
                self.entries = entries
            def __enter__(self):
                return self.entries
            def __exit__(self, *args):
                return False
        def scanning(descriptor):
            prefix = self.descriptors[descriptor].rstrip("/") + "/"
            names = [name[len(prefix):] for name in self.nodes
                     if name.startswith(prefix) and "/" not in name[len(prefix):] and name != prefix]
            return Listing(iter(SimpleNamespace(name=name) for name in names))
        with ExitStack() as stack:
            stack.enter_context(patch.object(toolchain, "authenticate_host_boundary"))
            stack.enter_context(patch.object(toolchain.os, "O_DIRECTORY", 65536, create=True))
            stack.enter_context(patch.object(toolchain.os, "O_NOFOLLOW", 131072, create=True))
            stack.enter_context(patch.object(toolchain.os, "open", side_effect=self.opening))
            stack.enter_context(patch.object(toolchain.os, "close", side_effect=lambda fd: self.descriptors.pop(fd)))
            stack.enter_context(patch.object(toolchain.os, "stat", side_effect=self.info))
            stack.enter_context(patch.object(toolchain.os, "fstat", side_effect=lambda fd: self.nodes[self.descriptors[fd]]))
            stack.enter_context(patch.object(toolchain.os, "scandir", side_effect=scanning))
            def readlink(name, dir_fd):
                target = self.info(name, dir_fd=dir_fd).target
                return target if isinstance(target, bytes) else target.encode()
            stack.enter_context(patch.object(toolchain.os, "readlink", side_effect=readlink))
            return (operation() if operation is not None else
                    toolchain.inspect_worker_toolchains(boundary=BOUNDARY, roots=roots))


class ToolTreeTests(unittest.TestCase):
    def test_complete_readonly_closure_preserves_internal_alias_and_stable_digest(self):
        fs = Filesystem()
        proof = fs.inspect()
        self.assertEqual(proof.roots, (ROOT,))
        self.assertEqual(proof.files, 3)
        self.assertEqual(proof.total_bytes, 18)
        self.assertEqual(len(proof.metadata_sha256), 64)
        self.assertEqual(proof, fs.inspect())
        self.assertEqual(fs.descriptors, {})

    def test_group_other_writable_foreign_owned_and_special_entries_are_rejected(self):
        for path, field, value in [(ROOT + "/lib/site.py", "st_mode", stat.S_IFREG | 0o666),
                                    (ROOT + "/lib", "st_mode", stat.S_IFDIR | 0o775),
                                    ("/opt/hostedtoolcache", "st_mode", stat.S_IFDIR | 0o777),
                                    (ROOT + "/bin/python3.11", "st_uid", 2000),
                                    (ROOT + "/bin/python3", "st_uid", 2000),
                                    (ROOT + "/lib/site.py", "st_mode", stat.S_IFIFO | 0o644)]:
            fs = Filesystem()
            setattr(fs.nodes[path], field, value)
            with self.subTest(path=path, field=field), self.assertRaises(MbError):
                fs.inspect()
            self.assertEqual(fs.descriptors, {})

    def test_links_cannot_escape_prefixes_or_exceed_hop_budget(self):
        for target in ("/home/runner/secret", "/tmp/candidate", "python3", b"\xff"):
            fs = Filesystem()
            fs.add(ROOT + "/bin/python3", stat.S_IFLNK | 0o777, target=target)
            with self.subTest(target=target), self.assertRaises(MbError):
                fs.inspect()
            self.assertEqual(fs.descriptors, {})

    def test_metadata_byte_file_entry_and_tree_caps_are_global(self):
        for name, cap in [("MAX_CI_SOURCE_FILES", 2), ("MAX_CI_SOURCE_ENTRIES", 6),
                          ("MAX_CI_SOURCE_FILE_BYTES", 4), ("MAX_CI_SOURCE_TREE_BYTES", 17),
                          ("MAX_CI_SOURCE_LIST_BYTES", 10), ("MAX_CI_TOOL_TREE_DEPTH", 0)]:
            fs = Filesystem()
            with self.subTest(bound=name), patch.object(limits, name, cap), self.assertRaises(MbError):
                fs.inspect()
            self.assertEqual(fs.descriptors, {})

    def test_empty_duplicate_foreign_and_oversized_root_inventories_are_rejected(self):
        for roots in [(), [], (ROOT, ROOT), ("/tmp/candidate",), ("/opt/hostedtoolcache",),
                      tuple(ROOT + str(index) for index in range(limits.MAX_CI_TOOL_ROOTS + 1))]:
            with self.subTest(roots=roots), self.assertRaises(MbError):
                Filesystem().inspect(roots)

    def test_changed_tool_receipt_is_not_authority(self):
        proof = Filesystem().inspect()
        changed = toolchain.ToolTreeProof(proof.roots, "a" * 64, proof.files, proof.entries, proof.total_bytes)
        with patch.object(toolchain, "inspect_worker_toolchains", return_value=proof):
            toolchain.authenticate_toolchains(proof, boundary=BOUNDARY)
            with self.assertRaises(MbError):
                toolchain.authenticate_toolchains(changed, boundary=BOUNDARY)
        with self.assertRaises(MbError):
            toolchain.authenticate_toolchains({}, boundary=BOUNDARY)

    def test_directory_alias_cycle_is_inspected_without_unbounded_recursion(self):
        fs = Filesystem()
        fs.add(ROOT + "/lib/loop", stat.S_IFLNK | 0o777, target=".")
        proof = fs.inspect()
        self.assertEqual(proof.files, 4)
        self.assertEqual(fs.descriptors, {})

    def test_tool_fence_binds_actual_dispatch_paths_and_forbids_failed_admission(self):
        proof = Filesystem().inspect()
        account = WorkerAccount("candidate", 2000, 2000, "/tmp/private")
        args = dict(account=account, boundary=BOUNDARY, tools=proof, command=(), python=ROOT + "/bin/python3",
                    java_home=None, identity={}, run_id=42, run_attempt=2, values={}, timeout_seconds=60)
        with patch.object(toolchain, "authenticate_toolchains"), \
                patch.object(toolchain, "_execution_tool_paths"), \
                patch.object(toolchain, "execute_isolated_worker", return_value="result") as launch:
            self.assertEqual(toolchain.execute_tool_fenced_worker(**args), "result")
            self.assertEqual(launch.call_args.kwargs["python"], args["python"])
        for changes, error in [({"python": "/tmp/candidate/python"}, None),
                                ({"java_home": "/tmp/candidate/java"}, None),
                                ({}, WorkerError("changed closure"))]:
            with self.subTest(changes=changes), patch.object(toolchain, "authenticate_toolchains", side_effect=error), \
                    patch.object(toolchain, "execute_isolated_worker") as launch, \
                    patch.object(toolchain, "terminate_worker") as terminate, self.assertRaises(MbError):
                toolchain.execute_tool_fenced_worker(**{**args, **changes})
            launch.assert_not_called()
            terminate.assert_called_once_with(account)

    def test_execution_alias_requires_its_destination_tree_to_be_enrolled(self):
        fs = Filesystem()
        external = "/opt/hostedtoolcache/OtherPython"
        fs.add(external, stat.S_IFDIR | 0o755)
        fs.add(external + "/python", stat.S_IFREG | 0o755, size=3)
        fs.add(ROOT + "/bin/python3", stat.S_IFLNK | 0o777, target=external + "/python")
        fs.add(ROOT + "/lib/java", stat.S_IFLNK | 0o777, target=external)
        proof = fs.inspect()
        # Metadata for the target is admitted by the link scanner; its import tree is not.
        with self.assertRaisesRegex(MbError, "destination escapes"):
            fs.inspect(operation=lambda: toolchain._execution_tool_paths(
                proof, BOUNDARY, ROOT + "/bin/python3", None))
        with self.assertRaisesRegex(MbError, "destination escapes"):
            fs.inspect(operation=lambda: toolchain._execution_tool_paths(
                proof, BOUNDARY, ROOT + "/bin/python3.11", ROOT + "/lib/java"))
        enrolled = fs.inspect((ROOT, external))
        fs.inspect(operation=lambda: toolchain._execution_tool_paths(
            enrolled, BOUNDARY, ROOT + "/bin/python3", ROOT + "/lib/java"))
        self.assertEqual(fs.descriptors, {})

    def test_execution_bindings_accept_internal_aliases_and_resolved_root_aliases(self):
        fs = Filesystem()
        alias = "/opt/hostedtoolcache/Alias"
        fs.add(alias, stat.S_IFLNK | 0o777, target=ROOT)
        for roots, python in (((ROOT,), ROOT + "/bin/python3"),
                              ((alias,), alias + "/bin/python3")):
            with self.subTest(roots=roots):
                proof = fs.inspect(roots)
                fs.inspect(operation=lambda: toolchain._execution_tool_paths(
                    proof, BOUNDARY, python, roots[0]))
                self.assertEqual(fs.descriptors, {})

    def test_execution_bindings_reject_wrong_types_permissions_and_empty_python(self):
        for path, mode, size, java in ((ROOT, stat.S_IFDIR | 0o755, 0, None),
                                      (ROOT + "/bin/python3.11", stat.S_IFREG | 0o644, 3, None),
                                      (ROOT + "/bin/python3.11", stat.S_IFREG | 0o750, 3, None),
                                      (ROOT + "/bin/python3.11", stat.S_IFREG | 0o755, 0, None),
                                      (ROOT + "/lib", stat.S_IFDIR | 0o750, 0, ROOT + "/lib"),
                                      (ROOT + "/lib/site.py", stat.S_IFREG | 0o644, 5, ROOT + "/lib/site.py")):
            fs = Filesystem()
            fs.nodes[path].st_mode, fs.nodes[path].st_size = mode, size
            proof = fs.inspect()
            python = path if java is None else ROOT + "/bin/python3"
            with self.subTest(path=path, mode=mode, size=size), self.assertRaises(MbError):
                fs.inspect(operation=lambda: toolchain._execution_tool_paths(proof, BOUNDARY, python, java))
            self.assertEqual(fs.descriptors, {})

    def test_destination_failure_never_launches_and_terminates_account(self):
        fs = Filesystem()
        external = "/opt/hostedtoolcache/OtherPython"
        fs.add(external, stat.S_IFDIR | 0o755)
        fs.add(external + "/python", stat.S_IFREG | 0o755, size=3)
        fs.add(ROOT + "/bin/python3", stat.S_IFLNK | 0o777, target=external + "/python")
        proof = fs.inspect()
        account = WorkerAccount("candidate", 2000, 2000, "/tmp/private")
        args = dict(account=account, boundary=BOUNDARY, tools=proof, command=(), python=ROOT + "/bin/python3",
                    java_home=None, identity={}, run_id=42, run_attempt=2, values={}, timeout_seconds=60)
        with patch.object(toolchain, "execute_isolated_worker") as launch, \
                patch.object(toolchain, "terminate_worker") as terminate, \
                self.assertRaisesRegex(MbError, "destination escapes"):
            fs.inspect(operation=lambda: toolchain.execute_tool_fenced_worker(**args))
        launch.assert_not_called()
        terminate.assert_called_once_with(account)
        with patch.object(toolchain, "execute_isolated_worker", return_value="result") as launch:
            args["tools"] = fs.inspect((ROOT, external))
            self.assertEqual(fs.inspect(operation=lambda: toolchain.execute_tool_fenced_worker(**args)), "result")
        launch.assert_called_once()
        with patch.object(toolchain._Scan, "resolve", side_effect=OSError("unavailable")), \
                self.assertRaisesRegex(MbError, "cannot bind"):
            toolchain._execution_tool_paths(proof, BOUNDARY, args["python"], None)
