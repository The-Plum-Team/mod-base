"""Privileged kit copy admission; Windows OS/copy seams are explicit, not Linux proof."""

import copy
import hashlib
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import installation
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerError
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.pin import MAX_KIT_BYTES, MAX_KIT_FILES, kit_tree_digest
from mod_base.runtime import build_invocation


BOUNDARY = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
SOURCE = Path("/home/runner/approved-kit")


def invocation():
    return build_invocation(Path(__file__).parent / "fixtures/mods/qs_like", None,
                            {"MOD_BASE_KIT_SHA": "a" * 40}, check_repository=False, root=SOURCE)


def inventory():
    records = {top: [{"path": "file.py", "mode": "100644", "size": 1,
                      "sha256": hashlib.sha256(top.encode()).hexdigest()}]
               for top in ("src", "site", "requirements")}
    return records, "sha256:" + "b" * 64, 3, 3


class KitInstallationTests(unittest.TestCase):
    def test_digest_listing_matches_existing_kit_contract_including_empty_source(self):
        self.assertEqual(limits.MAX_CI_KIT_INSTALL_FILES, MAX_KIT_FILES)
        self.assertEqual(limits.MAX_CI_KIT_INSTALL_BYTES, MAX_KIT_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for top, data in (("src", b""), ("site", b"site\x00"), ("requirements", b"lock\n")):
                (root / top).mkdir()
                (root / top / "file.py").write_bytes(data)
            def records(path, **kwargs):
                data = (path / "file.py").read_bytes()
                return [{"path": "file.py", "mode": "100644", "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}]
            with patch.object(installation, "_paths", return_value=(("file.py",), 1)), \
                    patch.object(installation, "source_records", side_effect=records):
                _, digest, files, total = installation._inventory(root)
            self.assertEqual(digest, kit_tree_digest(root))
            self.assertEqual((files, total), (3, 10))

    def test_global_file_byte_entry_and_source_mode_bounds(self):
        snapshot = inventory()[0]
        for bound, cap in (("MAX_CI_KIT_INSTALL_FILES", 2), ("MAX_CI_KIT_INSTALL_BYTES", 2)):
            with self.subTest(bound=bound), patch.object(limits, bound, cap), \
                    patch.object(installation, "_paths", return_value=(("file.py",), 1)), \
                    patch.object(installation, "source_records", side_effect=lambda path, **kw: snapshot[path.name]), \
                    self.assertRaises(MbError):
                installation._inventory(SOURCE)
        for mode in ("100755", "120000"):
            rows = copy.deepcopy(snapshot)
            rows["src"][0]["mode"] = mode
            with self.subTest(mode=mode), patch.object(installation, "_paths", return_value=(("file.py",), 1)), \
                    patch.object(installation, "source_records", side_effect=lambda path, **kw: rows[path.name]), \
                    self.assertRaises(MbError):
                installation._inventory(SOURCE)

    def _filesystem(self):
        # Imported locally so unittest discovery never duplicates another module's classes.
        from tests.test_ci_toolchain import Filesystem, ROOT
        fs = Filesystem()
        del fs.nodes[ROOT + "/bin/python3"]
        fs.nodes[ROOT + "/bin/python3.11"].st_mode = stat.S_IFREG | 0o644
        return fs, Path(ROOT)

    def _discover(self, fs, root, remaining=100):
        def dup(descriptor):
            copied = fs.next_fd
            fs.next_fd += 1
            fs.descriptors[copied] = fs.descriptors[descriptor]
            return copied
        with patch.object(installation.os, "dup", side_effect=dup):
            return fs.inspect(operation=lambda: installation._paths(root, remaining_entries=remaining))

    def test_bounded_descriptor_discovery_rejects_poison_links_modes_and_special_files(self):
        fs, root = self._filesystem()
        self.assertEqual(self._discover(fs, root), (("bin/python3.11", "lib/site.py"), 4))
        self.assertEqual(fs.descriptors, {})
        for name, mode, links in (("evil.pyc", stat.S_IFREG | 0o644, 1),
                                  ("evil.pth", stat.S_IFREG | 0o644, 1),
                                  ("__pycache__", stat.S_IFDIR | 0o755, 1),
                                  ("bad name", stat.S_IFREG | 0o644, 1),
                                  ("special", stat.S_IFIFO | 0o644, 1),
                                  ("executable", stat.S_IFREG | 0o755, 1),
                                  ("hardlink", stat.S_IFREG | 0o644, 2),
                                  ("symlink", stat.S_IFLNK | 0o777, 1)):
            fs, root = self._filesystem()
            fs.add(root.as_posix() + "/" + name, mode)
            fs.nodes[root.as_posix() + "/" + name].st_nlink = links
            with self.subTest(name=name), self.assertRaises(MbError):
                self._discover(fs, root)
            self.assertEqual(fs.descriptors, {})

    def test_discovery_caps_and_named_root_substitution_close_all_descriptors(self):
        for remaining, cap, value in ((0, None, None), (100, "MAX_CI_TOOL_TREE_DEPTH", 0),
                                     (100, "MAX_CI_KIT_INSTALL_FILES", 1)):
            fs, root = self._filesystem()
            with ExitStack() as stack:
                if cap:
                    stack.enter_context(patch.object(limits, cap, value))
                with self.subTest(cap=cap), self.assertRaises(MbError):
                    self._discover(fs, root, remaining)
            self.assertEqual(fs.descriptors, {})
        fs, root = self._filesystem()
        original = installation._open_directory
        roots = 0
        def opening(parts, **kwargs):
            nonlocal roots
            if parts == tuple(root.parts[1:]) and not kwargs:
                roots += 1
                if roots == 2:
                    fs.nodes[root.as_posix()].st_ino += 100
            return original(parts, **kwargs)
        with patch.object(installation, "_open_directory", side_effect=opening), \
                self.assertRaisesRegex(MbError, "replaced"):
            self._discover(fs, root)
        self.assertEqual(fs.descriptors, {})

    def test_installation_shape_is_closed_and_rebinds_the_named_root(self):
        for change in (None, "extra", "missing"):
            fs, root = self._filesystem()
            for name in list(fs.nodes):
                if name.startswith(root.as_posix() + "/"):
                    del fs.nodes[name]
            for top in ("src", "site", "requirements"):
                fs.add(root.as_posix() + "/" + top, stat.S_IFDIR | 0o700)
            if change == "extra":
                fs.add(root.as_posix() + "/other", stat.S_IFREG | 0o600)
            elif change == "missing":
                del fs.nodes[root.as_posix() + "/site"]
            with self.subTest(change=change), ExitStack() as stack:
                if change:
                    stack.enter_context(self.assertRaisesRegex(MbError, "root entries"))
                result = fs.inspect(operation=lambda: installation._shape(root))
                if not change:
                    self.assertEqual(result[:2], (1, fs.nodes[root.as_posix()].st_ino))
            self.assertEqual(fs.descriptors, {})

    def _install(self, *, snapshots=None, copied=None, role_error=None):
        state = inventory()
        with ExitStack() as stack:
            guard = stack.enter_context(patch.object(installation, "authenticate_privileged_host_boundary", side_effect=role_error))
            stack.enter_context(patch.object(installation, "_layout"))
            stack.enter_context(patch.object(installation, "_shape", return_value=(1, 11)))
            inspect = stack.enter_context(patch.object(installation, "_inventory", side_effect=snapshots or [state, state, state]))
            mkdir = stack.enter_context(patch.object(installation.os, "mkdir"))
            stack.enter_context(patch.object(installation, "_open_directory", return_value=17))
            stack.enter_context(patch.object(installation.os, "close"))
            stack.enter_context(patch.object(installation, "copy_source_files",
                side_effect=lambda path, fd, **kw: state[0][path.name] if copied is None else copied))
            metadata = stack.enter_context(patch.object(installation, "authenticate_tree_private_access"))
            publish = stack.enter_context(patch.object(installation, "atomic_directory",
                side_effect=lambda path, fill: fill(Path("/tmp/private-stage"), 31)))
            result = installation.install_privileged_kit(invocation(), boundary=BOUNDARY, expected_digest=state[1])
            self.assertEqual(guard.call_count, 2)
            self.assertEqual(inspect.call_count, 3)
            self.assertEqual(metadata.call_count, 2)
            for call in metadata.call_args_list:
                self.assertEqual(call.args, (Path("/tmp/private-stage"),))
                self.assertEqual(call.kwargs, dict(owner_uid=0, owner_gid=0,
                                                  max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES))
            self.assertEqual(mkdir.call_count, 3)
            self.assertEqual(publish.call_args.args[0], Path(str(installation.PRIVILEGED_KIT_ROOT)))
            return result

    def test_installation_is_fixed_private_independent_and_source_rechecked(self):
        self.assertEqual(self._install(), installation.KitInstallation("a" * 40, "1.0.3", inventory()[1], 3, 3, 1, 11))
        for index in (1, 2):
            snapshots = [inventory(), inventory(), inventory()]
            snapshots[index] = (*snapshots[index][:1], "sha256:" + "c" * 64, 3, 3)
            with self.subTest(changed=index), self.assertRaises(MbError):
                self._install(snapshots=snapshots)
        with self.assertRaises(MbError):
            self._install(copied=[])

    def test_roles_bad_invocations_paths_and_digest_reject_before_publication(self):
        with patch.object(installation, "authenticate_privileged_host_boundary", side_effect=WorkerError("not root")), \
                patch.object(installation, "_inventory") as inspect, \
                patch.object(installation, "atomic_directory") as publish, self.assertRaises(MbError):
            installation.install_privileged_kit(None, boundary=BOUNDARY, expected_digest="bad")
        inspect.assert_not_called()
        publish.assert_not_called()
        from dataclasses import replace
        for inv, digest in ((None, inventory()[1]), (invocation(), "bad"),
                            (replace(invocation(), kit_root=Path("/tmp/candidate")), inventory()[1])):
            with self.subTest(inv=inv, digest=digest), \
                    patch.object(installation, "authenticate_privileged_host_boundary"), \
                    patch.object(installation, "_inventory") as inspect, \
                    patch.object(installation, "atomic_directory") as publish, self.assertRaises(MbError):
                installation.install_privileged_kit(inv, boundary=BOUNDARY, expected_digest=digest)
            inspect.assert_not_called()
            publish.assert_not_called()
        with patch.object(installation, "authenticate_privileged_host_boundary", side_effect=OSError("denied")), \
                self.assertRaisesRegex(MbError, "cannot install"):
            installation.install_privileged_kit(invocation(), boundary=BOUNDARY, expected_digest=inventory()[1])

    def test_reauthentication_rejects_metadata_bytes_counts_types_and_io(self):
        state = inventory()
        receipt = installation.KitInstallation("a" * 40, "1.0.3", state[1], 3, 3, 1, 11)
        from dataclasses import replace
        for supplied, snapshot, error in ((receipt, state, None), (receipt, (*state[:2], 2, 3), None),
                                          (replace(receipt, files=True), state, None),
                                          (replace(receipt, digest="bad"), state, None),
                                          (replace(receipt, inode=12), state, None),
                                          (replace(receipt, device=True), state, None),
                                          (None, state, None), (receipt, state, WorkerError("metadata")),
                                          (receipt, state, OSError("metadata"))):
            good = supplied is receipt and snapshot == state and error is None
            with self.subTest(supplied=supplied, error=error), ExitStack() as stack:
                stack.enter_context(patch.object(installation, "authenticate_privileged_host_boundary"))
                stack.enter_context(patch.object(installation, "_layout"))
                stack.enter_context(patch.object(installation, "_shape", return_value=(1, 11)))
                stack.enter_context(patch.object(installation, "_inventory", return_value=snapshot))
                stack.enter_context(patch.object(installation, "authenticate_tree_private_access", side_effect=error))
                if not good:
                    stack.enter_context(self.assertRaises(MbError))
                installation.authenticate_privileged_kit(supplied, boundary=BOUNDARY)
