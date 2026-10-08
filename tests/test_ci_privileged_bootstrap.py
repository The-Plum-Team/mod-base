"""The root bootstrap's stdlib guard: closed arguments, mirrored constants and the real digest.

Everything here runs without root. The root-only half (the checkout walk as uid 0, the package
load and every operation) runs for real in ``tests/ci_linux_worker.py``.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mod_base
from mod_base.build_ci import host, root_request
from mod_base.model import grammar, limits
from mod_base.pin import DIGESTED_DIRS, MAX_KIT_BYTES, MAX_KIT_FILES, kit_tree_digest


KIT = Path(__file__).resolve().parents[1]
PROGRAM = KIT / "tools/ci_privileged_bootstrap.py"
spec = importlib.util.spec_from_file_location("ci_privileged_bootstrap_tests", PROGRAM)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
LINUX = sys.platform == "linux"


def entry_arguments(**changes):
    values = {"--operation": bootstrap.OPERATIONS[0], "--kit": "/home/runner/work/mod/mod/kit",
              "--kit-digest": "sha256:" + "b" * 64, "--nonce": "d" * 64, **changes}
    return [item for flag in bootstrap.ENTRY_FLAGS for item in (flag, values[flag])]


def run_program(arguments, *, isolated=True):
    flags = ("-I", "-B", "-S") if isolated else ("-B",)
    return subprocess.run((sys.executable, *flags, str(PROGRAM), *arguments), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=60, check=False)


class RootBootstrapTests(unittest.TestCase):
    def test_closed_arguments_accept_exactly_the_four_ordered_pairs(self):
        self.assertEqual(bootstrap._entry_arguments(entry_arguments()),
                         dict(zip(bootstrap.ENTRY_FLAGS, entry_arguments()[1::2])))
        for operation in bootstrap.OPERATIONS:
            self.assertEqual(bootstrap._entry_arguments(entry_arguments(**{"--operation": operation}))["--operation"],
                             operation)
        valid = entry_arguments()
        cases = [[], valid[:-1], valid + ["--command", "echo forged"], tuple(valid), valid[2:] + valid[:2]]
        for flag, value in (("--operation", "shell"), ("--operation", ""), ("--operation", "--help"),
                            ("--kit", "/tmp/kit"), ("--kit", "/home/runner"), ("--kit", "/home/runner/kit/../x"),
                            ("--kit", "/home/runner//kit"), ("--kit", "/home/runner/kit/"),
                            ("--kit", "/home/runner/k\nit"), ("--kit", "/home/runner/" + "x" * 4097),
                            ("--kit", "/home/runner/\ud800"), ("--kit", "/home/runner/a:b"),
                            ("--kit", "/home/runner/a\\b"), ("--kit", "home/runner/kit"),
                            ("--kit-digest", "b" * 64), ("--kit-digest", "sha256:" + "B" * 64),
                            ("--kit-digest", "sha256:" + "b" * 63), ("--kit-digest", "sha256:" + "b" * 64 + "\n"),
                            ("--nonce", "D" * 64), ("--nonce", "d" * 63), ("--nonce", True)):
            cases.append(entry_arguments(**{flag: value}))
        renamed = entry_arguments()
        renamed[0] = "--command"
        cases.append(renamed)
        for arguments in cases:
            with self.subTest(arguments=str(arguments)[:90]), self.assertRaises(bootstrap.BootstrapError):
                bootstrap._entry_arguments(arguments)

    def test_real_process_rejects_bad_arguments_and_the_unprivileged_runner_with_one_fixed_line(self):
        # A correct argument vector still fails the root/isolation guard before any kit byte is read.
        real = entry_arguments(**{"--kit": KIT.as_posix()}) if LINUX and KIT.as_posix().startswith("/home/runner/") \
            else entry_arguments()
        for arguments, isolated in (([], True), (["--help"], True), (entry_arguments()[:-1], True),
                                    (entry_arguments(**{"--operation": "shell"}), True),
                                    (entry_arguments(), True), (real, True), (real, False)):
            with self.subTest(arguments=str(arguments)[:60], isolated=isolated):
                result = run_program(arguments, isolated=isolated)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr.replace(b"\r\n", b"\n"), b"mod-base: root bootstrap rejected\n")

    def test_isolated_program_load_imports_no_kit_and_changes_no_search_path(self):
        body = ("import importlib.util,sys\noriginal=tuple(sys.path)\n"
                f"spec=importlib.util.spec_from_file_location('guard',{str(PROGRAM)!r})\n"
                "module=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                "assert tuple(sys.path)==original\n"
                "assert not any(n=='mod_base' or n.startswith('mod_base.') for n in sys.modules)\n")
        result = subprocess.run((sys.executable, "-I", "-B", "-S", "-c", body),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_independent_contract_constants_match_central_definitions(self):
        self.assertEqual(bootstrap.ROOTS, DIGESTED_DIRS)
        self.assertEqual(bootstrap.OPERATIONS, grammar.CI_ROOT_OPERATIONS)
        self.assertEqual(bootstrap.PROGRAM, root_request.ROOT_PROGRAM)
        self.assertEqual(bootstrap.RUNNER_HOME, host.HOST_RUNNER_HOME)
        self.assertEqual(bootstrap.REPOSITORY, mod_base.KIT_REPOSITORY)
        self.assertEqual((bootstrap.MAX_FILES, bootstrap.MAX_BYTES), (MAX_KIT_FILES, MAX_KIT_BYTES))
        for local, central in (("MAX_FILES", "MAX_CI_KIT_INSTALL_FILES"),
                               ("MAX_ENTRIES", "MAX_CI_KIT_INSTALL_ENTRIES"),
                               ("MAX_BYTES", "MAX_CI_KIT_INSTALL_BYTES"),
                               ("MAX_DEPTH", "MAX_CI_TOOL_TREE_DEPTH"),
                               ("MAX_UNIX_ID", "MAX_CI_UNIX_ID"),
                               ("MAX_ARGUMENT_BYTES", "MAX_CI_TOOL_PATH_BYTES"),
                               ("READ_BYTES", "CI_PROCESS_READ_BYTES"),
                               ("TIMEOUT_SECONDS", "CI_ROOT_OPERATION_TIMEOUT_SECONDS")):
            self.assertEqual(getattr(bootstrap, local), getattr(limits, central), local)
        for version in ("0.0.0", "1.0.3", "999999.999999.999999", "01.0.0", "1.0.0\n", "v1.0.0"):
            self.assertEqual(bool(bootstrap.VERSION.fullmatch(version)), bool(grammar.VERSION.fullmatch(version)))
        for digest in ("sha256:" + "a" * 64, "sha256:" + "A" * 64, "a" * 64, "sha256:" + "a" * 64 + "\n"):
            self.assertEqual(bool(bootstrap.DIGEST.fullmatch(digest)), bool(grammar.DIGEST.fullmatch(digest)))
        for nonce in ("a" * 64, "A" * 64, "a" * 63, "a" * 64 + "\n"):
            self.assertEqual(bool(bootstrap.NONCE.fullmatch(nonce)), bool(grammar.SHA256.fullmatch(nonce)))


@unittest.skipUnless(LINUX, "descriptor-relative no-follow walks are exercised on Linux")
class RootBootstrapDigestTests(unittest.TestCase):
    """The guard's own kit-digest-v1 over real trees, with the current user in the runner's role."""

    def digest(self, root):
        descriptor = os.open(root, bootstrap._DIRECTORY)
        try:
            return bootstrap._kit_digest(descriptor, os.getuid())
        finally:
            os.close(descriptor)

    def tree(self, base):
        root = Path(base) / "kit"
        for top in DIGESTED_DIRS:
            (root / top).mkdir(parents=True)
        (root / "src/mod_base").mkdir()
        (root / "src/mod_base/__init__.py").write_bytes(b"__version__ = '1.0.3'\n")
        (root / "src/mod_base/empty.py").write_bytes(b"")
        (root / "site/page.html").write_bytes(b"<p>inert</p>\n")
        (root / "requirements/pillow.txt").write_bytes(b"inert lock\n")
        for path in (root, *root.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        return root

    def test_this_checkout_has_the_digest_the_kit_computes_for_itself(self):
        before = set(os.listdir("/proc/self/fd"))
        self.assertEqual(self.digest(KIT), kit_tree_digest(KIT))
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_fixture_digest_equals_the_kit_listing_including_empty_files(self):
        with tempfile.TemporaryDirectory(prefix="mod-base-bootstrap-") as directory:
            root = self.tree(directory)
            self.assertEqual(self.digest(root), kit_tree_digest(root))
            (root / "site/page.html").write_bytes(b"<p>changed</p>\n")
            self.assertEqual(self.digest(root), kit_tree_digest(root))
            (root / "tools").mkdir()
            (root / "tools/unhashed.py").write_bytes(b"outside kit-digest-v1\n")
            self.assertEqual(self.digest(root), kit_tree_digest(root))

    def test_writable_executable_linked_bytecode_and_special_entries_are_refused(self):
        def world_writable(root):
            (root / "src/mod_base/empty.py").chmod(0o666)

        def group_writable_directory(root):
            (root / "src/mod_base").chmod(0o775)

        def executable(root):
            (root / "src/mod_base/empty.py").chmod(0o755)

        def bytecode_directory(root):
            (root / "src/mod_base/__pycache__").mkdir()

        def bytecode_file(root):
            (root / "src/mod_base/empty.pyc").write_bytes(b"")

        def path_file(root):
            (root / "src/hostile.pth").write_bytes(b"import os\n")

        def symlink(root):
            (root / "src/mod_base/link.py").symlink_to("empty.py")

        def directory_symlink(root):
            (root / "site").rmdir() if not any((root / "site").iterdir()) else None
            (root / "requirements/alias").symlink_to(root / "site", target_is_directory=True)

        def hardlink(root):
            os.link(root / "src/mod_base/empty.py", root / "site/alias.py")

        def fifo(root):
            os.mkfifo(root / "src/mod_base/pipe.py")

        def odd_name(root):
            (root / "src/mod_base/odd name.py").write_bytes(b"")

        def missing_root(root):
            (root / "requirements/pillow.txt").unlink()
            (root / "requirements").rmdir()

        def empty_kit(root):
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()

        for mutate in (world_writable, group_writable_directory, executable, bytecode_directory, bytecode_file,
                       path_file, symlink, directory_symlink, hardlink, fifo, odd_name, missing_root, empty_kit):
            with self.subTest(mutation=mutate.__name__), \
                    tempfile.TemporaryDirectory(prefix="mod-base-bootstrap-") as directory:
                root = self.tree(directory)
                mutate(root)
                before = set(os.listdir("/proc/self/fd"))
                with self.assertRaises((bootstrap.BootstrapError, OSError)):
                    self.digest(root)
                self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_file_and_byte_bounds_stop_the_walk(self):
        with tempfile.TemporaryDirectory(prefix="mod-base-bootstrap-") as directory:
            root = self.tree(directory)
            expected = self.digest(root)
            for name, value in (("MAX_FILES", 3), ("MAX_ENTRIES", 4), ("MAX_BYTES", 10), ("MAX_DEPTH", 0)):
                original = getattr(bootstrap, name)
                setattr(bootstrap, name, value)
                try:
                    with self.subTest(bound=name), self.assertRaises(bootstrap.BootstrapError):
                        self.digest(root)
                finally:
                    setattr(bootstrap, name, original)
            self.assertEqual(self.digest(root), expected)

    def test_checkout_is_opened_only_through_protected_real_directories(self):
        home = Path.home()
        with tempfile.TemporaryDirectory(prefix="mod-base-bootstrap-", dir=home) as directory:
            root = self.tree(directory)
            Path(directory).chmod(0o755)
            descriptor = bootstrap._open_kit(root.as_posix(), os.getuid())
            try:
                self.assertEqual(os.fstat(descriptor).st_ino, root.stat().st_ino)
            finally:
                os.close(descriptor)
            alias = Path(directory) / "alias"
            alias.symlink_to(root, target_is_directory=True)
            Path(directory).chmod(0o775)
            before = set(os.listdir("/proc/self/fd"))
            for path in (alias.as_posix(), (alias / "src").as_posix(), root.as_posix(),
                         (root / "missing").as_posix(), (root / "src/mod_base/__init__.py").as_posix()):
                with self.subTest(path=path), self.assertRaises((bootstrap.BootstrapError, OSError)):
                    os.close(bootstrap._open_kit(path, os.getuid()))
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        # A checkout below a world-writable directory is never admitted, sticky or not.
        with tempfile.TemporaryDirectory(prefix="mod-base-bootstrap-", dir="/tmp") as directory:
            root = self.tree(directory)
            with self.assertRaises(bootstrap.BootstrapError):
                os.close(bootstrap._open_kit(root.as_posix(), os.getuid()))


if __name__ == "__main__":
    unittest.main()
