"""Root request channel: the parts that need neither root nor accounts.

Publication, the root reader and every operation run for real, as the runner and as root, in
``tests/ci_linux_worker.py``. Nothing here starts ``sudo``: every launcher case is rejected before
a process exists.
"""

import os
import sys
import unittest
from pathlib import Path

from mod_base.build_ci import root_request, root_request_operations
from mod_base.build_ci.controller import authenticate_controller_sources
from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.worker import WORKER_ROOT
from mod_base.errors import MbError
from mod_base.model import grammar
from tests.helpers import ci_root_request
from tests.test_ci_controller import ControllerSourceTests


KIT = Path(__file__).resolve().parents[1]
NONCE = "a" * 64
DIGEST = "sha256:" + "b" * 64


class RootRequestChannelTests(unittest.TestCase):
    def test_each_operation_has_its_own_fixed_private_directory_below_the_worker_root(self):
        paths = [root_request.root_request_path(operation) for operation in grammar.CI_ROOT_OPERATIONS]
        self.assertEqual(len(set(paths)), len(paths))
        for operation, path in zip(grammar.CI_ROOT_OPERATIONS, paths):
            self.assertEqual(path.parent, WORKER_ROOT)
            self.assertEqual(path.name, f"root-request-{operation}")
        for operation in ("", "host", "../escape", "freeze-build-validation/..", None, 7, b"host-fence"):
            with self.subTest(operation=operation), self.assertRaises(MbError):
                root_request.root_request_path(operation)

    def test_the_bootstrap_named_by_the_launcher_exists_in_this_checkout(self):
        self.assertTrue((KIT / root_request.ROOT_PROGRAM).is_file())
        self.assertEqual(root_request._ROOT_ENV["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")

    def test_source_metadata_carries_identities_and_sizes_but_never_bytes(self):
        plan, api, _, protected = ControllerSourceTests().fixture(empty_module=True)
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        metadata = root_request._source_metadata(sources)
        self.assertEqual(set(metadata), {"controller_sha", "controller_tree", "config", "files"})
        self.assertEqual([row["path"] for row in metadata["files"]], [file.path for file in sources.files])
        for row, file in zip([metadata["config"], *metadata["files"]], [sources.config, *sources.files]):
            self.assertEqual(set(row), {"path", "mode", "git_blob", "sha256", "size"})
            self.assertEqual((row["size"], row["sha256"], row["git_blob"]), (len(file.data), file.sha256, file.git_blob))
        self.assertIn(0, [row["size"] for row in metadata["files"]])
        document = ci_root_request()
        document["arguments"]["sources"] = metadata
        document["arguments"]["plan"] = plan
        document["arguments"]["envelope"].update(identity=plan["identity"], plan_sha256=plan["plan_sha256"])
        self.assertEqual(validate_root_request(document)["arguments"]["sources"], metadata)

    @unittest.skipUnless(sys.platform == "linux", "the launcher and the root entry are Linux-only")
    def test_launcher_rejects_every_malformed_argument_before_starting_a_process(self):
        valid = dict(python=sys.executable, kit_root=KIT, kit_digest=DIGEST, nonce=NONCE)
        operation = grammar.CI_ROOT_OPERATIONS[0]
        for name, value in (("python", "python3"), ("python", "/usr/bin/../bin/python3"), ("python", "/usr/bin/py\nthon"),
                            ("kit_root", str(KIT)), ("kit_root", Path("relative/kit")),
                            ("kit_digest", "b" * 64), ("kit_digest", "sha256:" + "B" * 64), ("kit_digest", None),
                            ("nonce", "a" * 63), ("nonce", "A" * 64), ("nonce", None)):
            with self.subTest(name=name, value=value), self.assertRaises(MbError):
                root_request.run_root_operation(operation, **{**valid, name: value})
        for bad in ("", "freeze", "--help", None):
            with self.subTest(operation=bad), self.assertRaises(MbError):
                root_request.run_root_operation(bad, **valid)

    @unittest.skipUnless(sys.platform == "linux", "the launcher and the root entry are Linux-only")
    def test_root_only_entries_refuse_the_unprivileged_runner(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        operation = grammar.CI_ROOT_OPERATIONS[0]
        with self.assertRaisesRegex(MbError, "require protected Linux root"):
            root_request_operations.execute_root_operation(operation, kit_root=str(KIT), kit_digest=DIGEST, nonce=NONCE)
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            root_request.read_root_request(operation, nonce=NONCE)

    def test_the_dispatch_table_is_exactly_the_closed_operation_set(self):
        self.assertEqual(tuple(root_request_operations._OPERATIONS), grammar.CI_ROOT_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
