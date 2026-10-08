"""Required hosted Linux account tests, invoked explicitly by every Python CI matrix leg.

Not named test_*: ordinary local discovery must not allocate accounts on arbitrary hosts.
Explicit execution rejects missing Linux/hosted-runner/sudo prerequisites instead of skipping.
This exercises current primitives, not the unfinished copy/execute/freeze/validator lifecycle.
"""

from __future__ import annotations

import os
import hashlib
import shutil
import stat
import struct
import site
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import identity, lifecycle, worker
from mod_base.build_ci.worker import (WORKER_ACCOUNTS, WORKER_ROOT, WorkerExecutionError,
                                      allocate_worker_account, authenticate_worker_account,
                                      prepare_worker_boundary, terminate_worker)
from tests import ci_lifecycle_fixture as job_fixture
from tests import ci_mod_harness as h
from tests.helpers import ci_plan, ci_plan_inputs
from tests.helpers import ci_envelope
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, materialize_build_export, verify_build_export
from mod_base.io.tree import copy_regular_files
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.model import limits
from mod_base.build_ci.source import materialize_source_copy, parse_source_inventory, verify_source_copy
from mod_base.build_ci.host import (authenticate_host_boundary, execute_isolated_worker,
                                    protect_worker_host)
from mod_base.build_ci.toolchain import (_execution_tool_paths, execute_tool_fenced_worker,
                                        inspect_worker_toolchains)
from mod_base.errors import MbError
from mod_base.io.tree import copy_source_files, source_records


HOST_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
KIT = Path(__file__).resolve().parents[1]
#: The interpreter's import roots: its installation and, when it runs from one, its virtual
#: environment. On a hosted runner both are the one setup-python prefix.
PYTHON_ROOTS = tuple(dict.fromkeys((sys.base_prefix, sys.prefix)))
#: The candidate files ``ci_plan()`` binds, by staged name: a validator's input root holds them
#: next to the plan.
_, SOURCES = ci_plan_inputs()
_FENCED = []


def fence_host(boundary):
    """Run the real root operation: this checkout's bootstrap, through sudo, for a published request."""
    from mod_base.build_ci.root_request import request_host_fence, run_root_operation
    from mod_base.pin import kit_tree_digest
    digest = kit_tree_digest(KIT)
    nonce = request_host_fence(boundary=boundary)
    started = time.monotonic()
    try:
        run_root_operation("host-fence", python=sys.executable, kit_root=KIT, kit_digest=digest, nonce=nonce)
    finally:
        # The first line of a run is the cost of closing the image; later ones only walk it.
        print(f"host fence root operation: {time.monotonic() - started:.2f}s", file=sys.stderr)


def fence_host_once(boundary):
    """The fence changes the host for good; the first fixture of a process pays for it.

    A failure is kept as well: every later fixture then fails at once with the first error
    instead of walking the image again.
    """
    if not _FENCED:
        try:
            fence_host(boundary)
        except MbError as error:
            _FENCED.append(error)
            raise
        _FENCED.append(None)
    elif _FENCED[0] is not None:
        raise AssertionError("the host fence of this process already failed") from _FENCED[0]


def command(*args, accepted=(0,), cwd=None):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20, env=HOST_ENV, check=False, cwd=cwd)
    if result.returncode not in accepted:
        raise AssertionError(f"Linux fixture command {args[0]} failed ({result.returncode})")
    return result


class HostedWorkerCase(unittest.TestCase):
    """A fresh boundary, the runner's home fence, the host fence and (by default) both accounts."""

    allocate = True

    def setUp(self):
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" \
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("UID integration tests require a fresh GitHub-hosted Linux runner")
        import pwd

        command("/usr/bin/sudo", "-n", "/usr/bin/true")
        self.root = Path(str(WORKER_ROOT))
        self.boundary = self.root.parent
        self.accounts = []
        self.processes = []
        self.host_processes = []
        self.host_boundary = None
        if self.boundary.exists() or self.boundary.is_symlink():
            raise AssertionError("Linux fixture boundary already exists; refusing to repurpose it")
        for name in WORKER_ACCOUNTS.values():
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            raise AssertionError("Linux fixture account already exists; refusing to repurpose it")
        prepare_worker_boundary(runner_environment="github-hosted")
        self.addCleanup(self.cleanup)
        self.host_boundary = protect_worker_host(runner_environment="github-hosted", runner_home="/home/runner",
                                                  workspace=os.environ["GITHUB_WORKSPACE"],
                                                  runner_temp=os.environ["RUNNER_TEMP"])
        if self.allocate:
            fence_host_once(self.host_boundary)
            self.allocate_accounts()
        self.private = self.boundary / "private-controller-probe"
        self.private.write_bytes(b"inert private fixture")
        self.private.chmod(0o600)
        probes = tempfile.TemporaryDirectory(prefix="mod-base-host-probe-", dir=os.environ["RUNNER_TEMP"])
        self.addCleanup(probes.cleanup)
        probe_root = Path(probes.name)
        probe_root.chmod(0o755)
        self.host_marker = probe_root / "public-fixture-only-marker"
        self.host_marker.write_bytes(b"inert host boundary fixture")
        self.host_marker.chmod(0o644)

    def allocate_accounts(self):
        for role in WORKER_ACCOUNTS:
            account = allocate_worker_account(role)
            self.accounts.append((role, account.uid, account.gid))

    def cleanup(self):
        import pwd

        errors = []
        for role, uid, gid in reversed(self.accounts):
            try:
                record = pwd.getpwnam(WORKER_ACCOUNTS[role])
                if (record.pw_uid, record.pw_gid) != (uid, gid):
                    raise AssertionError("fixture account was reassigned; refusing to delete it")
                terminate_worker(authenticate_worker_account(role))
                command("/usr/bin/sudo", "-n", "/usr/sbin/userdel", WORKER_ACCOUNTS[role])
            except Exception as error:
                errors.append(error)
        for process in self.processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired as error:
                errors.append(error)
        for process in self.host_processes:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
        for name in WORKER_ACCOUNTS.values():
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            errors.append(AssertionError("fixture account remains; refusing to relax the host fence"))
        if errors:
            raise AssertionError("Linux fixture did not terminate/clean every account") from errors[0]
        # Both known identities have gone; reclaim only the dedicated fresh fixture boundary.
        command("/usr/bin/sudo", "-n", "/usr/bin/chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(self.boundary))
        shutil.rmtree(self.boundary)
        if self.host_boundary is not None:
            authenticate_host_boundary(self.host_boundary)
            os.chmod(self.host_boundary.home, self.host_boundary.original_mode, follow_symlinks=False)

    def as_worker(self, role, *args, accepted=(0,)):
        account = authenticate_worker_account(role)
        return command("/usr/bin/sudo", "-n", "--user", f"#{account.uid}", "--", "/usr/bin/setpriv",
                       "--no-new-privs", "--", "/usr/bin/env", "-i", f"--chdir={account.home}",
                       "PATH=/usr/bin:/bin", *args, accepted=accepted, cwd=self.root)


class LinuxHostFenceTests(HostedWorkerCase):
    """The real root fence over a stand-in with a hosted image's modes, before any account exists."""

    allocate = False

    def setUp(self):
        super().setUp()
        self.planted = []
        self.addCleanup(lambda: [command("/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(path))
                                 for path in self.planted])

    def plant(self, parent):
        """A runner-owned tree as the image build leaves the tool cache: 0777, default ACL other::rwx."""
        root = Path(parent) / f"mod-base-fence-standin-{os.getpid()}-{len(self.planted)}"
        self.planted.append(root)
        command("/usr/bin/sudo", "-n", "/usr/bin/mkdir", "--", str(root))
        command("/usr/bin/sudo", "-n", "/usr/bin/chown", f"{os.getuid()}:{os.getgid()}", "--", str(root))
        (root / "Python/3.99.0/x64/bin").mkdir(parents=True)
        (root / "Python/3.99.0/x64/lib").mkdir()
        (root / "Python/3.99.0/x64/bin/python3").write_bytes(b"inert stand-in interpreter")
        (root / "Python/3.99.0/x64/lib/os.py").write_bytes(b"inert stand-in module\n")
        (root / "Python/3.99.0/x64/bin/python").symlink_to("python3")
        (root / "Ruby").mkdir()
        command("/usr/bin/setfacl", "-R", "-d", "-m", "u::rwx,u:runner:rwx,g::rwx,m::rwx,o::rwx", str(root))
        # What is installed below a default ACL inherits an access ACL with a named user and a mask.
        (root / "Python/3.99.0/x64/lib/inherited").mkdir()
        (root / "Python/3.99.0/x64/lib/inherited/module.py").write_bytes(b"inert stand-in module\n")
        for path in (root, *root.rglob("*")):
            if not path.is_symlink():
                path.chmod(0o777)
        (root / "Ruby").chmod(0o1777)
        return root

    def test_hosted_like_tree_is_closed_before_accounts_and_no_worker_can_write_it(self):
        root = self.plant("/opt")
        prefix = root / "Python/3.99.0/x64"
        inherited = prefix / "lib/inherited/module.py"
        self.assertIn("system.posix_acl_default", os.listxattr(prefix))
        self.assertIn("system.posix_acl_access", os.listxattr(inherited))
        with self.assertRaisesRegex(MbError, "group/other write"):
            inspect_worker_toolchains(boundary=self.host_boundary, roots=(str(prefix),))
        fence_host(self.host_boundary)
        # The inherited access ACL stays, capped by its mask: mode bits are all a later check needs.
        self.assertIn("system.posix_acl_access", os.listxattr(inherited))
        self.assertEqual(stat.S_IMODE(inherited.stat().st_mode), 0o755)
        for path in (root, *root.rglob("*")):
            if path.is_symlink():
                continue
            info = path.stat()
            self.assertEqual(stat.S_IMODE(info.st_mode) & 0o022, 0, path)
            self.assertEqual(info.st_uid, os.getuid())
            if path.is_dir():
                self.assertNotIn("system.posix_acl_default", os.listxattr(path), path)
        self.assertEqual(stat.S_IMODE((root / "Ruby").stat().st_mode), 0o1755)
        self.assertEqual(stat.S_IMODE((prefix / "bin/python3").stat().st_mode), 0o755)
        self.assertEqual((prefix / "bin/python3").read_bytes(), b"inert stand-in interpreter")
        (prefix / "lib/created-after").mkdir()
        (prefix / "lib/created-after.py").write_bytes(b"")
        for name in ("lib/created-after", "lib/created-after.py"):
            self.assertEqual(stat.S_IMODE((prefix / name).stat().st_mode) & 0o022, 0)
        # The same stand-in is now an admissible tool root, wherever it lives.
        proof = inspect_worker_toolchains(boundary=self.host_boundary, roots=(str(prefix),))
        self.assertEqual(proof.files, 5)
        _execution_tool_paths(proof, self.host_boundary, str(prefix / "bin/python"), str(prefix))
        for sticky in ("/tmp", "/var/tmp"):
            self.assertEqual(stat.S_IMODE(Path(sticky).stat().st_mode), 0o1777)
        self.allocate_accounts()
        for role in WORKER_ACCOUNTS:
            for path in (root, prefix, prefix / "bin", prefix / "bin/python3", prefix / "lib/os.py", inherited,
                         inherited.parent, root / "Ruby", Path("/opt"), Path("/usr/local/bin"), Path("/usr/share")):
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.as_worker(role, "/usr/bin/test", "-w", str(path), accepted=(0, 1)).returncode, 1)
            self.assertEqual(self.as_worker(role, "/usr/bin/cat", str(prefix / "lib/os.py")).stdout,
                             b"inert stand-in module\n")
            self.assertEqual(self.as_worker(role, "/usr/bin/touch", str(prefix / "lib/planted.pth"),
                                            accepted=(0, 1)).returncode, 1)
            self.assertEqual(self.as_worker(role, "/usr/bin/test", "-w", "/tmp").returncode, 0)

    def test_anything_world_writable_left_outside_the_fenced_trees_fails_closed(self):
        from mod_base.build_ci.root_request import root_request_path
        for kind in ("directory", "file"):
            with self.subTest(kind=kind):
                stray = Path("/var/lib") / f"mod-base-fence-stray-{os.getpid()}-{kind}"
                self.planted.append(stray)
                command("/usr/bin/sudo", "-n", "/usr/bin/mkdir", "--", str(stray))
                leaf = stray if kind == "directory" else stray / "leaf"
                if kind == "file":
                    command("/usr/bin/sudo", "-n", "/usr/bin/touch", "--", str(leaf))
                command("/usr/bin/sudo", "-n", "/usr/bin/chmod", "0777" if kind == "directory" else "0666", "--", str(leaf))
                with self.assertRaises(MbError) as caught:
                    fence_host(self.host_boundary)
                self.assertIn("world-writable", str(caught.exception))
                self.assertIn(str(leaf), str(caught.exception))
                self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o777 if kind == "directory" else 0o666)
                command("/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(stray))
                # One request per operation and boundary: the failed one is never reused.
                shutil.rmtree(Path(str(root_request_path("host-fence"))))
        # A world-writable file only its existing owner can reach is no entry for a later account.
        hidden = Path(os.environ["RUNNER_TEMP"]) / f"mod-base-fence-hidden-{os.getpid()}"
        hidden.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, hidden)
        (hidden / "cache.lock").write_bytes(b"")
        (hidden / "cache.lock").chmod(0o666)
        fence_host(self.host_boundary)

    def test_default_acl_the_fence_cannot_remove_fails_the_operation_with_the_tool_diagnostic(self):
        root = self.plant("/opt")
        prefix = root / "Python/3.99.0/x64"
        locked = prefix / "lib"
        locked.chmod(0o755)  # No write bit is left on it: only the default ACL, which the proof cannot see.
        command("/usr/bin/sudo", "-n", "/usr/bin/chattr", "+i", "--", str(locked))
        self.addCleanup(command, "/usr/bin/sudo", "-n", "/usr/bin/chattr", "-i", "--", str(locked))
        with self.assertRaisesRegex(MbError, "host fence command failed: setfacl: .*Operation not permitted"):
            fence_host(self.host_boundary)
        self.assertIn("system.posix_acl_default", os.listxattr(locked))
        # Everything else was closed, and the tool scan refuses the directory the fence could not finish.
        self.assertEqual(stat.S_IMODE((prefix / "bin/python3").stat().st_mode), 0o755)
        with self.assertRaisesRegex(MbError, "default ACL"):
            inspect_worker_toolchains(boundary=self.host_boundary, roots=(str(prefix),))

    def test_fence_refuses_to_run_once_a_worker_account_exists(self):
        fence_host_once(self.host_boundary)
        self.allocate_accounts()
        shutil.rmtree(Path(str(WORKER_ROOT / "root-request-host-fence")), ignore_errors=True)
        with self.assertRaisesRegex(MbError, "before any worker account exists"):
            fence_host(self.host_boundary)


class LinuxWorkerTests(HostedWorkerCase):
    def test_existing_boundary_is_not_adopted_or_replaced(self):
        with self.assertRaises(MbError):
            prepare_worker_boundary(runner_environment="github-hosted")
        self.assertEqual(stat.S_IMODE(self.boundary.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o711)

    def test_host_home_workspace_temp_and_public_marker_are_inaccessible(self):
        authenticate_host_boundary(self.host_boundary)
        self.assertEqual(stat.S_IMODE(Path(self.host_boundary.home).stat().st_mode), 0o700)
        paths = (self.host_boundary.home, os.environ["GITHUB_WORKSPACE"], os.environ["RUNNER_TEMP"],
                 str(self.host_marker))
        for role in WORKER_ACCOUNTS:
            for path in paths:
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.as_worker(role, "/usr/bin/test", "-r", path, accepted=(0, 1)).returncode, 1)

    def test_python_binary_and_import_root_ancestors_are_not_worker_writable(self):
        roots = [Path(sys.executable), Path(os.__file__), *(Path(path) for path in site.getsitepackages())]
        paths = set()
        for root in roots:
            for path in (root, root.resolve()):
                paths.update((path, *path.parents))
        for role in WORKER_ACCOUNTS:
            for path in sorted(paths):
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.as_worker(role, "/usr/bin/test", "-w", str(path), accepted=(0, 1)).returncode, 1)

    def test_complete_real_python_tool_tree_is_admitted_before_fenced_dispatch(self):
        proof = inspect_worker_toolchains(boundary=self.host_boundary, roots=PYTHON_ROOTS)
        self.assertGreater(proof.files, 0)
        self.assertGreater(proof.entries, proof.files)
        _execution_tool_paths(proof, self.host_boundary, sys.executable, sys.base_prefix)
        with self.assertRaisesRegex(MbError, "regular file"):
            _execution_tool_paths(proof, self.host_boundary, sys.base_prefix, None)
        with self.assertRaisesRegex(MbError, "JAVA_HOME"):
            _execution_tool_paths(proof, self.host_boundary, sys.executable, sys.executable)
        result = self.run_dispatcher("print('readonly tools admitted',flush=True)\n", tools=proof)
        self.assertIn(b"readonly tools admitted", result.log)

    def test_root_bootstrap_admits_only_this_protected_checkout_at_its_exact_digest(self):
        from mod_base.pin import kit_tree_digest
        kit = Path(__file__).resolve().parents[1]
        digest = kit_tree_digest(kit)
        program = kit / "tools/ci_privileged_bootstrap.py"
        nonce = "0" * 64

        def root(*arguments, flags=("-I", "-B", "-S"), script=program, accepted=(2,)):
            return command("/usr/bin/sudo", "-n", "--", sys.executable, *flags, str(script), *arguments,
                           accepted=accepted, cwd=self.root)

        def entry(kit_root=kit, kit_digest=digest, operation="freeze-build-validation"):
            return ("--operation", operation, "--kit", str(kit_root), "--kit-digest", kit_digest, "--nonce", nonce)

        def rejected_before_import(result):
            self.assertEqual((result.stdout, result.stderr), (b"", b"mod-base: root bootstrap rejected\n"))

        rejected_before_import(root(*entry(kit_digest="sha256:" + "0" * 64)))
        rejected_before_import(root(*entry(), flags=("-B",)))
        rejected_before_import(root(*entry(), flags=("-I", "-S")))
        rejected_before_import(root(*entry(operation="shell")))
        rejected_before_import(root(*entry()[:-2]))
        with tempfile.TemporaryDirectory(prefix="mod-base-kit-copy-", dir=os.environ["RUNNER_TEMP"]) as directory:
            base = Path(directory)
            base.chmod(0o755)

            def copy(name):
                target = base / name
                for top in ("src", "site", "requirements", "tools", "template", "actions"):
                    shutil.copytree(kit / top, target / top)
                self.assertEqual(kit_tree_digest(target), digest)
                return target

            # An identical copy is another checkout: it runs its own program, never this one's.
            foreign = copy("foreign")
            rejected_before_import(root(*entry(), script=foreign / "tools/ci_privileged_bootstrap.py"))
            rejected_before_import(root(*entry(kit_root=foreign)))
            writable = copy("writable")
            (writable / "src/mod_base/build_ci").chmod(0o775)
            rejected_before_import(root(*entry(kit_root=writable), script=writable / "tools/ci_privileged_bootstrap.py"))
            loose = copy("loose")
            (loose / "src/mod_base/errors.py").chmod(0o666)
            rejected_before_import(root(*entry(kit_root=loose), script=loose / "tools/ci_privileged_bootstrap.py"))
            cached = copy("cached")
            (cached / "src/mod_base/__pycache__").mkdir()
            rejected_before_import(root(*entry(kit_root=cached), script=cached / "tools/ci_privileged_bootstrap.py"))
            changed = copy("changed")
            (changed / "src/mod_base/errors.py").write_bytes(b"raise SystemExit(0)\n")
            rejected_before_import(root(*entry(kit_root=changed), script=changed / "tools/ci_privileged_bootstrap.py"))
            alias = base / "alias"
            alias.symlink_to(foreign, target_is_directory=True)
            rejected_before_import(root(*entry(kit_root=alias), script=alias / "tools/ci_privileged_bootstrap.py"))
            # The admitted copy loads and then fails closed in kit code: no request names this nonce.
            result = root(*entry(kit_root=foreign), script=foreign / "tools/ci_privileged_bootstrap.py")
            self.assertEqual(result.stdout, b"")
            self.assertRegex(result.stderr, rb"\Amod_base: [^\n]+\n\Z")
            self.assertFalse(any(base.rglob("__pycache__/*")))
        self.assertFalse(any((kit / "src").rglob("__pycache__")))

    def test_other_uid_cannot_read_host_process_environment_memory_or_ptrace(self):
        probe = subprocess.Popen((sys.executable, "-I", "-B", "-c", "import time;time.sleep(120)"),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={**HOST_ENV, "MB_HOST_CANARY": "inert fixture value"}, cwd=self.boundary)
        self.host_processes.append(probe)
        body = ("import ctypes,errno,os\n"
                f"pid={probe.pid}\n"
                "for name in ('environ','mem'):\n"
                "    try:\n        descriptor=os.open(f'/proc/{pid}/{name}',os.O_RDONLY)\n"
                "    except PermissionError:\n        pass\n"
                "    else:\n        os.close(descriptor)\n        raise AssertionError('host proc access admitted')\n"
                "libc=ctypes.CDLL(None,use_errno=True)\n"
                "libc.ptrace.argtypes=[ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p]\n"
                "libc.ptrace.restype=ctypes.c_long\n"
                "result=libc.ptrace(16,pid,None,None)\n"
                "assert result == -1 and ctypes.get_errno() in (errno.EPERM,errno.EACCES)\n"
                "print('host process denied',flush=True)\n")
        self.assertIsNone(probe.poll())
        for role in WORKER_ACCOUNTS:
            result = self.run_dispatcher(body, role=role)
            self.assertIn(b"host process denied", result.log)
            self.assertIsNone(probe.poll())

    def test_even_an_inheritable_host_descriptor_is_closed_before_dispatch(self):
        descriptor = os.open(self.host_marker, os.O_RDONLY)
        try:
            os.set_inheritable(descriptor, True)
            result = self.run_dispatcher("import errno,os\n"
                                         f"try:\n    os.fstat({descriptor})\n"
                                         "except OSError as error:\n    assert error.errno == errno.EBADF\n"
                                         "else:\n    raise AssertionError('host descriptor inherited')\n"
                                         "print('host descriptor closed',flush=True)\n")
            self.assertIn(b"host descriptor closed", result.log)
        finally:
            os.close(descriptor)

    def test_accounts_are_distinct_and_cannot_access_controller_or_other_home(self):
        candidate = authenticate_worker_account("candidate")
        validator = authenticate_worker_account("validator")
        self.assertNotEqual(candidate.uid, validator.uid)
        self.assertNotEqual(candidate.gid, validator.gid)
        for role in WORKER_ACCOUNTS:
            with self.assertRaises(MbError):
                allocate_worker_account(role)
        for role, other in (("candidate", validator), ("validator", candidate)):
            self.assertEqual(self.as_worker(role, "/usr/bin/id", "-u").stdout.strip(),
                             str(authenticate_worker_account(role).uid).encode())
            result = self.as_worker(role, "/usr/bin/test", "-r", str(self.private), accepted=(0, 1))
            self.assertEqual(result.returncode, 1)
            result = self.as_worker(role, "/usr/bin/test", "-r", other.home, accepted=(0, 1))
            self.assertEqual(result.returncode, 1)
            result = self.as_worker(role, "/usr/bin/sudo", "-n", "/usr/bin/true", accepted=(0, 1))
            self.assertEqual(result.returncode, 1)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o600)

    def test_read_handoff_allows_only_validator_reads_and_no_worker_writes(self):
        validator = authenticate_worker_account("validator")
        candidate = authenticate_worker_account("candidate")
        root = Path(str(BUILD_VALIDATION_ROOT))
        root.mkdir(mode=0o700)
        envelope = ci_envelope()
        for record in envelope["files"]:
            path = root / record["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (record["role"] + " inert independent export bytes").encode()
            path.write_bytes(data)
            record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        leaf = root / envelope["files"][0]["path"]
        leaf_bytes = leaf.read_bytes()
        # Linux UAPI layout/tags: include/uapi/linux/posix_acl{,_xattr}.h at v6.8.
        # Plant named-user grants masked off by 0700/0600; chmod alone would reactivate them.
        def acl(owner_permissions, mask_permissions):
            undefined = (1 << 32) - 1
            entries = ((0x01, owner_permissions, undefined), (0x02, owner_permissions, candidate.uid),
                       (0x04, 0, undefined), (0x10, mask_permissions, undefined), (0x20, 0, undefined))
            return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
        os.setxattr(root, "system.posix_acl_access", acl(7, 0))
        os.setxattr(root, "system.posix_acl_default", acl(7, 7))
        os.setxattr(leaf, "system.posix_acl_access", acl(6, 0))
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o600)
        self.assertIn("system.posix_acl_access", os.listxattr(root))
        source = Path(__file__).resolve().parents[1] / "src"
        # Only reviewed fixture code and the protected kit source enter this root process.
        # Neither a candidate dispatcher nor any candidate import path is used.
        body = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                "from mod_base.build_ci.host import HostBoundary\n"
                "from mod_base.build_ci.worker import WorkerAccount\n"
                "from mod_base.build_ci.exports import prepare_build_validation\n"
                f"prepare_build_validation(boundary={self.host_boundary!r},validator={validator!r},"
                f"plan={ci_plan()!r})\n")
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c", body, cwd=self.root)
        self.assert_quiescent("candidate")
        self.assertEqual((root.stat().st_uid, root.stat().st_gid, stat.S_IMODE(root.stat().st_mode)),
                         (os.getuid(), validator.gid, 0o750))
        self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                         (os.getuid(), validator.gid, 0o640))
        for path in (root, leaf):
            self.assertNotIn("system.posix_acl_access", os.listxattr(path))
            self.assertNotIn("system.posix_acl_default", os.listxattr(path))
        self.assertEqual(self.as_worker("validator", "/usr/bin/cat", str(leaf)).stdout,
                         leaf_bytes)
        # Probe the locked UID directly through protected root setpriv, independent of PAM expiry.
        def candidate_probe(*args, accepted=(0,)):
            return command("/usr/bin/sudo", "-n", "--", "/usr/bin/setpriv", "--no-new-privs",
                           f"--reuid={candidate.uid}", f"--regid={candidate.gid}", "--clear-groups", "--",
                           "/usr/bin/env", "-i", f"--chdir={candidate.home}", "PATH=/usr/bin:/bin", *args,
                           accepted=accepted, cwd=self.root)
        self.assertEqual(candidate_probe("/usr/bin/id", "-u").stdout.strip(), str(candidate.uid).encode())
        self.assertEqual(candidate_probe("/usr/bin/test", "-r", str(leaf), accepted=(0, 1)).returncode, 1)
        for role in WORKER_ACCOUNTS:
            probe = candidate_probe if role == "candidate" else lambda *args, **kwargs: self.as_worker(role, *args, **kwargs)
            self.assertEqual(probe("/usr/bin/test", "-w", str(root), accepted=(0, 1)).returncode, 1)
            self.assertEqual(probe("/usr/bin/test", "-w", str(leaf), accepted=(0, 1)).returncode, 1)

    def test_controller_handoff_includes_empty_sources_and_allows_only_validator_reads(self):
        from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT,
                                                  authenticate_controller_sources, materialize_controller_sources)
        from tests.test_ci_controller import ControllerSourceTests
        validator = authenticate_worker_account("validator")
        candidate = authenticate_worker_account("candidate")
        plan, api, _, protected = ControllerSourceTests().fixture(
            empty_module=True, alter=lambda config, rows: rows[0].update(mode="100755"))
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        root = Path(str(CONTROLLER_VALIDATION_ROOT))
        materialize_controller_sources(root, sources=sources, identity=plan["identity"])
        source = Path(__file__).resolve().parents[1] / "src"
        body = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                "from mod_base.build_ci.host import HostBoundary\n"
                "from mod_base.build_ci.worker import WorkerAccount\n"
                "from mod_base.build_ci.controller import ControllerFile,ControllerSources,prepare_controller_validation\n"
                f"prepare_controller_validation(boundary={self.host_boundary!r},validator={validator!r},"
                f"sources={sources!r},identity={plan['identity']!r})\n")
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c", body, cwd=self.root)
        self.assert_quiescent("candidate")
        self.assertEqual((root.stat().st_uid, root.stat().st_gid, stat.S_IMODE(root.stat().st_mode)),
                         (os.getuid(), validator.gid, 0o750))
        def candidate_probe(*args, accepted=(0,)):
            return command("/usr/bin/sudo", "-n", "--", "/usr/bin/setpriv", "--no-new-privs",
                           f"--reuid={candidate.uid}", f"--regid={candidate.gid}", "--clear-groups", "--",
                           "/usr/bin/env", "-i", f"--chdir={candidate.home}", "PATH=/usr/bin:/bin", *args,
                           accepted=accepted, cwd=self.root)
        self.assertEqual(candidate_probe("/usr/bin/id", "-u").stdout.strip(), str(candidate.uid).encode())
        self.assertEqual(self.as_worker("validator", "/usr/bin/test", "-w", str(root), accepted=(0, 1)).returncode, 1)
        for file in (sources.config, *sources.files):
            leaf = root / file.path
            self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                             (os.getuid(), validator.gid, 0o640))
            self.assertEqual(self.as_worker("validator", "/usr/bin/cat", str(leaf)).stdout, file.data)
            self.assertEqual(self.as_worker("validator", "/usr/bin/test", "-w", str(leaf), accepted=(0, 1)).returncode, 1)
            self.assertEqual(candidate_probe("/usr/bin/test", "-r", str(leaf), accepted=(0, 1)).returncode, 1)
            self.assertEqual(candidate_probe("/usr/bin/test", "-w", str(leaf), accepted=(0, 1)).returncode, 1)

    def test_protected_controller_executes_fixed_hook_as_validator_and_locks_uid(self):
        self.protected_verifier_export("verify_build", None)

    def test_protected_target_verifier_reads_only_its_partition_and_freezes_bound_receipt(self):
        self.protected_verifier_export("verify_target", "target-a")

    def protected_verifier_export(self, hook, unit_id):
        from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT, authenticate_controller_sources,
                                                  execute_controller_validator, materialize_controller_sources)
        from mod_base.build_ci.inputs import (VALIDATOR_INPUT_ROOT, execute_frozen_build_validator,
                                              materialize_validation_inputs)
        from tests.test_ci_controller import ControllerSourceTests
        from tests.helpers import ci_validation
        from mod_base.build_ci.validation import SEALED_VALIDATION_ROOT, VALIDATOR_OUTPUT_ROOT, verify_validation_export
        from mod_base import __version__
        from mod_base.pin import kit_tree_digest
        from mod_base.model.canonical import canonical_sha256
        from tests.helpers import ci_plan
        kit = Path(__file__).resolve().parents[1]
        digest = kit_tree_digest(kit)
        executing_plan = ci_plan()
        executing_plan["identity"]["kit"].update(version=__version__, tree_digest=digest)
        executing_plan["plan_sha256"] = canonical_sha256({key:value for key,value in executing_plan.items() if key!="plan_sha256"})
        validator = authenticate_worker_account("validator")
        receipt = ci_validation(hook, unit_id)
        receipt["identity"] = executing_plan["identity"]
        receipt["plan_sha256"] = executing_plan["plan_sha256"]
        for report in receipt["reports"]:  # What the contract makes a verifier leave: `<unit id>.json`.
            report["path"] = report["unit_id"] + ".json"
        # The hook writes its reports and nothing else: the record is root's, built from them.
        native = ("import os,sys\n"
                  f"assert os.getuid()=={validator.uid} and os.geteuid()=={validator.uid}\n"
                  "assert sys.flags.isolated and sys.dont_write_bytecode\n"
                  f"assert sys.argv[1:]==['--hook',{hook!r}]\n"
                  f"assert os.environ.get('MB_TARGET_ID')=={unit_id!r}\n"
                  "assert 'MB_LANE_ID' not in os.environ\n"
                  f"assert os.getcwd()=={str(CONTROLLER_VALIDATION_ROOT)!r}\n"
                  "assert 'GITHUB_TOKEN' not in os.environ and 'GITHUB_ENV' not in os.environ\n"
                  "import json,hashlib\nfrom pathlib import Path\n"
                  "def encode(value):\n    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\\n').encode()\n"
                  f"receipt={receipt!r}\n"
                  "receipt['source_config_sha256']=hashlib.sha256(Path('scripts/ci/mod-base-build.json').read_bytes()).hexdigest()\n"
                  f"plan=json.loads(Path({str(VALIDATOR_INPUT_ROOT / grammar.CI_PLAN_NAME)!r}).read_bytes())\n"
                  "assert plan['plan_sha256']==receipt['plan_sha256'] and plan['identity']==receipt['identity']\n"
                  f"build=Path({str(BUILD_VALIDATION_ROOT)!r})\n"
                  f"raw=(build/{grammar.CI_ENVELOPE_NAME!r}).read_bytes()\n"
                  "receipt['input_sha256']=hashlib.sha256(raw).hexdigest()\n"
                  "envelope=json.loads(raw)\nassert envelope['plan_sha256']==plan['plan_sha256']\n"
                  f"assert envelope['scope']=={'target' if hook == 'verify_target' else 'complete'!r} and envelope['target_id']=={unit_id!r}\n"
                  "for path in (build,Path(" + repr(str(VALIDATOR_INPUT_ROOT / grammar.CI_PLAN_NAME)) + ")):\n"
                  "    assert not os.access(path,os.W_OK)\n"
                  "for file in envelope['files']:\n    data=(build/file['path']).read_bytes()\n"
                  "    assert len(data)==file['size'] and hashlib.sha256(data).hexdigest()==file['sha256']\n"
                  "root=Path(os.environ['HOME'])/'validation'\nroot.mkdir(mode=0o700)\n"
                  "for report in receipt['reports']:\n"
                  "    path=root/report['path']\n    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)\n"
                  "    path.write_bytes(encode({'fixture_unit':report['unit_id']}))\n    path.chmod(0o600)\n"
                  "print('inert-validator-hook',flush=True)\n").encode()
        plan, api, _, protected = ControllerSourceTests().fixture(source_data=native, empty_module=True)
        plan = executing_plan
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        materialize_controller_sources(Path(str(CONTROLLER_VALIDATION_ROOT)), sources=sources, identity=plan["identity"])
        materialize_validation_inputs(Path(str(VALIDATOR_INPUT_ROOT)), sources=SOURCES, plan=plan)
        envelope = ci_envelope()
        envelope["identity"] = plan["identity"]
        envelope["plan_sha256"] = plan["plan_sha256"]
        if hook == "verify_target":
            envelope.update(scope="target", target_id=unit_id)
        build = Path(str(BUILD_VALIDATION_ROOT))
        build.mkdir(mode=0o700)
        for file in envelope["files"]:
            data = (file["role"] + " synthetic validator input").encode()
            file.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            parent = build
            for part in Path(file["path"]).parts[:-1]:
                parent = parent / part
                parent.mkdir(mode=0o700, exist_ok=True)
            path = build / file["path"]
            path.write_bytes(data)
            path.chmod(0o600)
        (build / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        (build / grammar.CI_ENVELOPE_NAME).chmod(0o600)
        source = Path(__file__).resolve().parents[1] / "src"
        setup = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                 "from mod_base.build_ci.host import HostBoundary\n"
                 "from mod_base.build_ci.worker import WorkerAccount\n"
                 "from mod_base.build_ci.controller import ControllerFile,ControllerSources,prepare_controller_validation\n"
                 "from mod_base.build_ci.exports import prepare_build_validation\n"
                 "from mod_base.build_ci.inputs import prepare_validation_plan\n"
                 f"prepare_controller_validation(boundary={self.host_boundary!r},validator={validator!r},"
                 f"sources={sources!r},identity={plan['identity']!r})\n"
                 f"prepare_build_validation(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n"
                 f"prepare_validation_plan(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n")
        setup = "from pathlib import Path\n" + setup
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", setup, cwd=self.root)
        tools = inspect_worker_toolchains(boundary=self.host_boundary, roots=PYTHON_ROOTS)
        bound = execute_frozen_build_validator(
            boundary=self.host_boundary, validator=validator, plan=plan, envelope=envelope, hook=hook,
            unit_id=unit_id, run_id=42, run_attempt=2,
            execute=lambda: execute_controller_validator(
                boundary=self.host_boundary, validator=validator, sources=sources, tools=tools, plan=plan,
                hook=hook, unit_id=unit_id, python=sys.executable, java_home=None, run_id=42, run_attempt=2))
        result = bound.execution
        from mod_base.build_ci.handoff import EXECUTION_HANDOFF_ROOT, record_build_validation_execution
        nonce = record_build_validation_execution(boundary=self.host_boundary, sources=sources,
                    bound=bound, plan=plan, envelope=envelope, run_id=42, run_attempt=2)
        from mod_base.build_ci.root_request import (request_build_validation_freeze, root_request_path,
                                                    run_root_operation)
        operation = "freeze-build-validation"
        entry_nonce = request_build_validation_freeze(boundary=self.host_boundary, validator=validator,
            sources=sources, plan=plan, envelope=envelope, run_id=42, run_attempt=2, execution_nonce=nonce)
        with self.assertRaises(MbError):  # One request per operation: an existing channel is never reused.
            request_build_validation_freeze(boundary=self.host_boundary, validator=validator,
                sources=sources, plan=plan, envelope=envelope, run_id=42, run_attempt=2, execution_nonce=nonce)
        execution_root = Path(str(EXECUTION_HANDOFF_ROOT))
        self.assertEqual(stat.S_IMODE(execution_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((execution_root / grammar.CI_EXECUTION_NAME).stat().st_mode), 0o600)
        self.assertEqual(bound.input_sha256, hashlib.sha256(canonical_json(envelope)).hexdigest())
        receipt["input_sha256"] = bound.input_sha256
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"inert-validator-hook", result.log)
        self.assert_quiescent("candidate")
        self.assert_quiescent("validator")
        shadow = command("/usr/bin/sudo", "-n", "/usr/bin/getent", "shadow", WORKER_ACCOUNTS["validator"]).stdout.split(b":")
        self.assertTrue(shadow[1].startswith(b"!"))
        self.assertEqual(shadow[7], b"1")
        self.assertFalse(any(Path(str(CONTROLLER_VALIDATION_ROOT)).rglob("*.pyc")))
        # The real root entry: the bootstrap of this verified checkout re-computes the kit digest,
        # imports only from it and seals the receipt named by the runner's private request.
        frozen = Path(str(SEALED_VALIDATION_ROOT))
        for wrong in ({"nonce": "0" * 64}, {"kit_digest": "sha256:" + "0" * 64},
                      {"operation": "freeze-runtime-validation"}):
            with self.subTest(wrong=wrong), self.assertRaises(MbError):
                run_root_operation(**{"operation": operation, "python": sys.executable, "kit_root": kit,
                                      "kit_digest": digest, "nonce": entry_nonce, **wrong})
            self.assertFalse(frozen.exists())
        run_root_operation(operation, python=sys.executable, kit_root=kit, kit_digest=digest, nonce=entry_nonce)
        original = Path(str(VALIDATOR_OUTPUT_ROOT))
        expected = {**receipt, "source_config_sha256": sources.config.sha256}
        context = {"plan": plan, **{key: expected[key] for key in
                   ("hook", "unit_id", "run_id", "run_attempt", "source_config_sha256", "input_sha256")}}
        self.assertEqual(verify_validation_export(frozen, **context), expected)
        self.assertEqual((frozen.stat().st_uid, frozen.stat().st_gid, stat.S_IMODE(frozen.stat().st_mode)),
                         (os.getuid(), os.getgid(), 0o700))
        for leaf in frozen.rglob("*"):
            self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                             (os.getuid(), os.getgid(), 0o700 if leaf.is_dir() else 0o600))
        for role in WORKER_ACCOUNTS:
            account = authenticate_worker_account(role)
            def probe(*args, accepted=(0,)):
                return command("/usr/bin/sudo", "-n", "--", "/usr/bin/setpriv", "--no-new-privs",
                               f"--reuid={account.uid}", f"--regid={account.gid}", "--clear-groups", "--",
                               "/usr/bin/env", "-i", f"--chdir={account.home}", "PATH=/usr/bin:/bin", *args,
                               accepted=accepted, cwd=self.root)
            self.assertEqual(probe("/usr/bin/id", "-u").stdout.strip(), str(account.uid).encode())
            self.assertEqual(probe("/usr/bin/test", "-r", str(execution_root / grammar.CI_EXECUTION_NAME),
                                   accepted=(0, 1)).returncode, 1)
            self.assertEqual(probe("/usr/bin/test", "-r",
                                   str(root_request_path(operation) / grammar.CI_ROOT_REQUEST_NAME),
                                   accepted=(0, 1)).returncode, 1)
            for flag in ("-r", "-w"):
                self.assertEqual(probe("/usr/bin/test", flag, str(frozen), accepted=(0, 1)).returncode, 1)
        # Protected original remains validator-owned; the copy has independent source inodes.
        for report in expected["reports"]:
            info = command("/usr/bin/sudo", "-n", "/usr/bin/stat", "-c", "%u:%g:%i", str(original / report["path"])).stdout.decode().strip().split(":")
            self.assertEqual(tuple(map(int, info[:2])), (validator.uid, validator.gid))
            self.assertNotEqual(int(info[2]), (frozen / report["path"]).stat().st_ino)

    def test_private_execution_handoff_rejects_links_modes_oversize_nonce_and_json(self):
        # Data-channel fixture only: the retained object below is synthetic. The two existing
        # target/aggregate tests exercise this channel with actual returned worker execution.
        from mod_base.build_ci.controller import authenticate_controller_sources
        from mod_base.build_ci.handoff import EXECUTION_HANDOFF_ROOT, record_build_validation_execution
        from mod_base.build_ci.inputs import BuildValidationExecution
        from mod_base.build_ci.worker import WorkerResult
        from tests.test_ci_controller import ControllerSourceTests
        plan, api, _, protected = ControllerSourceTests().fixture()
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        envelope = ci_envelope()
        bound = BuildValidationExecution(WorkerResult(0, b"inert handoff metadata fixture", False),
                                         hashlib.sha256(canonical_json(envelope)).hexdigest())
        args = dict(boundary=self.host_boundary, sources=sources, bound=bound, plan=plan,
                    envelope=envelope, run_id=42, run_attempt=2)
        nonce = record_build_validation_execution(**args)
        root = Path(str(EXECUTION_HANDOFF_ROOT))
        leaf = root / grammar.CI_EXECUTION_NAME
        original = leaf.read_bytes()
        validator = authenticate_worker_account("validator")
        source = Path(__file__).resolve().parents[1] / "src"

        def rejected(message, supplied_nonce=nonce, *, replace_during_read=False):
            mutation = ""
            if replace_during_read:
                mutation = (
                    "import os,stat\nfrom pathlib import Path\n"
                    f"leaf=Path({str(leaf)!r})\noriginal=leaf.stat()\nraw=leaf.read_bytes()\n"
                    "original_read=os.read\nchanged=False\n"
                    "def swapping_read(descriptor,size):\n"
                    " global changed\n data=original_read(descriptor,size)\n info=os.fstat(descriptor)\n"
                    " if data and not changed and stat.S_ISREG(info.st_mode) and "
                    "(info.st_dev,info.st_ino)==(original.st_dev,original.st_ino):\n"
                    "  changed=True\n  replacement=leaf.with_name('replacement-fixture')\n"
                    "  replacement.write_bytes(raw)\n  replacement.chmod(0o600)\n"
                    f"  os.chown(replacement,{os.getuid()},{os.getgid()})\n"
                    "  os.replace(replacement,leaf)\n return data\nos.read=swapping_read\n")
            body = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                    "from mod_base.build_ci.host import HostBoundary\n"
                    "from mod_base.build_ci.worker import WorkerAccount\n"
                    "from mod_base.build_ci.controller import ControllerFile,ControllerSources\n"
                    "from mod_base.build_ci.handoff import freeze_handed_off_build_validation\n"
                    "from mod_base.errors import MbError\n"
                    + mutation +
                    "try:\n"
                    f" freeze_handed_off_build_validation(boundary={self.host_boundary!r},validator={validator!r},"
                    f"sources={sources!r},plan={plan!r},envelope={envelope!r},run_id=42,run_attempt=2,"
                    f"nonce={supplied_nonce!r})\n"
                    "except MbError as error:\n"
                    f" assert {message!r} in str(error), 'wrong handoff rejection'\n"
                    "else:\n raise AssertionError('unsafe execution handoff was admitted')\n")
            command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c", body, cwd=self.root)
            self.assert_quiescent("validator")

        with self.assertRaises(MbError):
            record_build_validation_execution(**args)
        self.assertEqual(leaf.read_bytes(), original)
        rejected("differs from protected attempt", "f" * 64 if nonce != "f" * 64 else "e" * 64)
        leaf.chmod(0o640)
        rejected("execution record must have private protected ownership")
        leaf.chmod(0o600)
        root.chmod(0o750)
        rejected("execution record must have private protected ownership")
        root.chmod(0o700)
        duplicate = self.root / "execution-hardlink-fixture"
        os.link(leaf, duplicate)
        rejected("linked, empty or oversized")
        duplicate.unlink()
        leaf.unlink()
        leaf.symlink_to(self.private)
        rejected("execution record must have private protected ownership")
        leaf.unlink()
        leaf.write_bytes(original)
        leaf.chmod(0o600)
        with leaf.open("wb") as stream:
            stream.truncate(limits.MAX_CI_EXECUTION_BYTES + 1)
        rejected("linked, empty or oversized")
        leaf.write_bytes(b" " + original)
        rejected("must be canonical JSON")
        leaf.write_bytes(original)
        rejected("execution record changed while read", replace_during_read=True)
        self.assertEqual(leaf.read_bytes(), original)
        candidate = authenticate_worker_account("candidate")
        command("/usr/bin/sudo", "-n", "--", "/usr/bin/chown", f"{candidate.uid}:{candidate.gid}", str(leaf), cwd=self.root)
        rejected("execution record must have private protected ownership")
        command("/usr/bin/sudo", "-n", "--", "/usr/bin/chown", f"{os.getuid()}:{os.getgid()}", str(leaf), cwd=self.root)
        self.assertEqual(leaf.read_bytes(), original)

    def test_uid_children_are_killed_reaped_and_account_is_expired(self):
        account = authenticate_worker_account("candidate")
        process = subprocess.Popen(
            ("/usr/bin/sudo", "-n", "--user", f"#{account.uid}", "--", "/usr/bin/setpriv", "--no-new-privs",
             "--", "/usr/bin/env", "-i", f"--chdir={account.home}", "PATH=/usr/bin:/bin", "/bin/bash", "--noprofile", "--norc", "-c",
             "sleep 300 & sleep 300 & wait"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=HOST_ENV, cwd=self.root)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pids = command("/usr/bin/pgrep", "-u", str(account.uid), accepted=(0, 1)).stdout.split()
            if len(pids) >= 3:
                break
            time.sleep(0.05)
        else:
            self.fail("real UID fixture did not spawn its shell and two children")
        with patch.object(worker, "_control", wraps=worker._control) as controls:
            terminate_worker(account)
        calls = [call.args[0] for call in controls.call_args_list]
        locked = next(index for index, args in enumerate(calls) if "--lock" in args)
        expected_sweep = [
            ("/usr/bin/sudo", "-n", "/usr/bin/pkill", "-KILL", flag, str(account.uid))
            for _ in range(2) for flag in ("-u", "-U")
        ] + [("/usr/bin/pgrep", flag, str(account.uid)) for flag in ("-u", "-U")]
        post_lock = calls[locked + 1:]
        self.assertGreaterEqual(len(post_lock), len(expected_sweep))
        self.assertEqual(len(post_lock) % len(expected_sweep), 0)
        for offset in range(0, len(post_lock), len(expected_sweep)):
            self.assertEqual(post_lock[offset:offset + len(expected_sweep)], expected_sweep)
        process.wait(timeout=20)
        for flag in ("-u", "-U"):
            self.assertEqual(command("/usr/bin/pgrep", flag, str(account.uid), accepted=(0, 1)).stdout, b"")
        shadow = command("/usr/bin/sudo", "-n", "/usr/bin/getent", "shadow", WORKER_ACCOUNTS["candidate"]).stdout.split(b":")
        self.assertTrue(shadow[1].startswith(b"!"))
        self.assertEqual(shadow[7], b"1")  # 1970-01-02: expiry is independently observable.

    def run_dispatcher(self, body, *, timeout=60, extra=(), role="candidate", tools=None, run_id=42):
        account = authenticate_worker_account(role)
        directory = self.root / ("repository" if role == "candidate" else "controller")
        scripts = directory / "scripts" / "ci"
        scripts.mkdir(parents=True, mode=0o755)
        dispatcher = scripts / "fixture_dispatch.py"
        dispatcher.write_text(body, encoding="utf-8")
        if role == "candidate":
            directory.chmod(0o700)
            command("/usr/bin/sudo", "-n", "/usr/bin/chown", "-hR", f"{account.uid}:{account.gid}", str(directory))
        else:
            directory.chmod(0o750)
            command("/usr/bin/sudo", "-n", "/usr/bin/chgrp", str(account.gid), str(directory))
        execute = execute_isolated_worker if tools is None else execute_tool_fenced_worker
        arguments = {} if tools is None else {"tools": tools}
        return execute(account, boundary=self.host_boundary, **arguments,
                              command=(sys.executable, "-I", "-B", str(dispatcher), *extra), python=sys.executable,
                              java_home=None, identity=ci_plan()["identity"], run_id=run_id, run_attempt=2,
                              values={}, timeout_seconds=timeout)

    def assert_quiescent(self, role="candidate"):
        account = authenticate_worker_account(role)
        for flag in ("-u", "-U"):
            self.assertEqual(command("/usr/bin/pgrep", flag, str(account.uid), accepted=(0, 1)).stdout, b"")

    def candidate_freeze_program(self, body, execution):
        """Protected synthetic tracked inventory; no live Git/API or compiler assertion."""
        from mod_base.build_ci.source import GitSourceEntry
        data = body.encode("utf-8")
        blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        inventory = (GitSourceEntry("scripts/ci/fixture_dispatch.py", "100644", len(data), blob),)
        source = Path(__file__).resolve().parents[1] / "src"
        candidate = authenticate_worker_account("candidate")
        return (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                "from mod_base.build_ci.host import HostBoundary\n"
                "from mod_base.build_ci.worker import WorkerAccount,WorkerResult\n"
                "from mod_base.build_ci.source import GitSourceEntry,SourceError\n"
                "from mod_base.build_ci.exports import freeze_build_export\n"
                f"def freeze():\n    return freeze_build_export(boundary={self.host_boundary!r},candidate={candidate!r},"
                f"execution={execution!r},inventory={inventory!r},generated_roots=(),plan={ci_plan()!r})\n")

    def test_runtime_candidate_copy_preserves_empty_data_and_grants_only_validator_reads(self):
        self.runtime_candidate_copy()

    def test_runtime_candidate_hardlink_is_rejected_before_private_copy(self):
        self.runtime_candidate_copy(hardlink=True)

    def runtime_candidate_copy(self, *, hardlink=False):
        """Real UID/source/copy/grant checks; synthetic bytes are not native/SDK/API approval."""
        from mod_base.build_ci.inputs import VALIDATOR_INPUT_ROOT, materialize_validation_inputs
        from mod_base.build_ci.runtime_inputs import RUNTIME_VALIDATION_ROOT
        from mod_base.build_ci.runtime_exports import verify_runtime_export
        from mod_base.build_ci.source import GitSourceEntry
        from tests.helpers import ci_runtime_envelope
        plan, build = ci_plan(), ci_envelope()
        runtime = ci_runtime_envelope()
        runtime.update(scope='lane', lane_id='lane-a')
        payloads = {'lanes/lane-a/result.json': b'{"fixture":"runtime"}\n',
                    'lanes/lane-a/runtime.log': b'', 'lanes/lane-a/crash.txt': b''}
        runtime['files'] = [{'path': name, 'lane_id': 'lane-a',
            'role': 'native-report' if name.endswith('.json') else ('runtime-log' if name.endswith('.log') else 'crash-report'),
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in sorted(payloads.items())]
        materialize_validation_inputs(Path(str(VALIDATOR_INPUT_ROOT)), sources=SOURCES, plan=plan)
        build_root = Path(str(BUILD_VALIDATION_ROOT))
        build_root.mkdir(mode=0o700)
        for row in build['files']:
            data = (row['role'] + ' synthetic runtime owning Build').encode()
            row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            path = build_root / row['path']
            parent = build_root
            for part in Path(row['path']).parts[:-1]:
                parent = parent / part
                parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        (build_root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(build))
        (build_root / grammar.CI_ENVELOPE_NAME).chmod(0o600)
        candidate = authenticate_worker_account('candidate')
        validator = authenticate_worker_account('validator')
        source = Path(__file__).resolve().parents[1] / 'src'
        prefix = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                  "from pathlib import Path\n"
                  "from mod_base.build_ci.host import HostBoundary\n"
                  "from mod_base.build_ci.worker import WorkerAccount,WorkerResult\n")
        preparation = (prefix +
            "from mod_base.build_ci.exports import prepare_build_validation\n"
            "from mod_base.build_ci.inputs import prepare_validation_plan\n"
            f"prepare_build_validation(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n"
            f"prepare_validation_plan(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n")
        command('/usr/bin/sudo', '-n', '--', sys.executable, '-I', '-B', '-S', '-c', preparation, cwd=self.root)
        body = ("import os\nfrom pathlib import Path\n"
                f"assert os.getuid()==os.geteuid()=={candidate.uid}\n"
                "assert os.environ['MB_RUN_ID']=='43' and os.environ['MB_RUN_ATTEMPT']=='2'\n"
                "assert 'GITHUB_TOKEN' not in os.environ and 'GITHUB_ENV' not in os.environ\n"
                "root=Path(os.environ['HOME'])/'export'\nroot.mkdir(mode=0o700)\n"
                f"payloads={payloads!r}\n"
                "for name,data in payloads.items():\n"
                "    path=root/name\n    parent=root\n"
                "    for part in Path(name).parts[:-1]:\n        parent=parent/part\n        parent.mkdir(mode=0o700,exist_ok=True)\n"
                "    path.write_bytes(data)\n    path.chmod(0o600)\n"
                f"path=root/{grammar.CI_RUNTIME_ENVELOPE_NAME!r}\n"
                f"path.write_bytes({canonical_json(runtime)!r})\npath.chmod(0o600)\n"
                "print('synthetic runtime export complete',flush=True)\n")
        execution = self.run_dispatcher(body, run_id=43)
        self.assertEqual(execution.returncode, 0)
        self.assertIn(b'synthetic runtime export complete', execution.log)
        self.assert_quiescent('candidate')
        data = body.encode('utf-8')
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        inventory = (GitSourceEntry('scripts/ci/fixture_dispatch.py', '100644', len(data), blob),)
        original = Path(candidate.home) / 'export'
        if hardlink:
            command('/usr/bin/sudo', '-n', '--', '/usr/bin/ln', str(original / 'lanes/lane-a/result.json'),
                    str(self.root / 'runtime-original-hardlink'), cwd=self.root)
        freeze = (prefix + "from mod_base.build_ci.source import GitSourceEntry\n"
                  "from mod_base.build_ci.runtime_freeze import freeze_runtime_export\n"
                  "from mod_base.errors import MbError\n"
                  f"def freeze():\n    return freeze_runtime_export(boundary={self.host_boundary!r},candidate={candidate!r},"
                  f"execution={execution!r},inventory={inventory!r},generated_roots=(),plan={plan!r},build={build!r},"
                  f"owning_build={runtime['owning_build']!r},lane_id='lane-a',run_id=43,run_attempt=2)\n")
        if hardlink:
            freeze += ("try:\n    freeze()\nexcept MbError as error:\n"
                       "    assert 'read-only tree permissions or identity changed' in str(error)\n"
                       "else:\n    raise AssertionError('linked original runtime was accepted')\n"
                       f"assert not Path({str(RUNTIME_VALIDATION_ROOT)!r}).exists()\n")
        else:
            freeze += f"assert freeze()=={runtime!r}\n"
        command('/usr/bin/sudo', '-n', '--', sys.executable, '-I', '-B', '-S', '-c', freeze, cwd=self.root)
        self.assert_quiescent('candidate')
        if hardlink:
            self.assertFalse(Path(str(RUNTIME_VALIDATION_ROOT)).exists())
            return
        frozen = Path(str(RUNTIME_VALIDATION_ROOT))
        self.assertEqual(verify_runtime_export(frozen, plan=plan), runtime)
        for leaf in (frozen, *frozen.rglob('*')):
            self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                (os.getuid(), os.getgid(), 0o700 if leaf.is_dir() else 0o600))
        for name, expected in payloads.items():
            private = frozen / name
            self.assertEqual(private.read_bytes(), expected)
            before = command('/usr/bin/sudo', '-n', '--', '/usr/bin/stat', '-c', '%u:%g:%i', str(original / name)).stdout.split(b':')
            self.assertEqual(tuple(map(int, before[:2])), (candidate.uid, candidate.gid))
            self.assertNotEqual(int(before[2]), private.stat().st_ino)
        grant = (prefix + "from mod_base.build_ci.runtime_inputs import prepare_runtime_validation\n"
            f"assert prepare_runtime_validation(boundary={self.host_boundary!r},validator={validator!r},"
            f"plan={plan!r},build={build!r},runtime={runtime!r},lane_id='lane-a',run_id=43,run_attempt=2)=={runtime!r}\n")
        command('/usr/bin/sudo', '-n', '--', sys.executable, '-I', '-B', '-S', '-c', grant, cwd=self.root)
        for leaf in (frozen, *frozen.rglob('*')):
            self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                (os.getuid(), validator.gid, 0o750 if leaf.is_dir() else 0o640))
        for role in WORKER_ACCOUNTS:
            account = authenticate_worker_account(role)
            def probe(*args, accepted=(0,)):
                return command('/usr/bin/sudo', '-n', '--', '/usr/bin/setpriv', '--no-new-privs',
                    f'--reuid={account.uid}', f'--regid={account.gid}', '--clear-groups', '--',
                    '/usr/bin/env', '-i', f'--chdir={account.home}', 'PATH=/usr/bin:/bin', *args,
                    accepted=accepted, cwd=self.root)
            for name, expected in payloads.items():
                path = str(frozen / name)
                self.assertEqual(probe('/usr/bin/test', '-w', path, accepted=(0, 1)).returncode, 1)
                if role == 'validator':
                    self.assertEqual(probe('/usr/bin/cat', path).stdout, expected)
                else:
                    self.assertEqual(probe('/usr/bin/test', '-r', path, accepted=(0, 1)).returncode, 1)
            self.assertEqual(probe('/usr/bin/test', '-w', str(frozen), accepted=(0, 1)).returncode, 1)
        for name in payloads:
            info = command('/usr/bin/sudo', '-n', '--', '/usr/bin/stat', '-c', '%u:%g', str(original / name)).stdout.strip()
            self.assertEqual(info, f'{candidate.uid}:{candidate.gid}'.encode())
        self.assert_quiescent('candidate')
        self.assert_quiescent('validator')

    def test_candidate_build_freeze_preserves_originals_and_excludes_both_worker_uids(self):
        envelope = ci_envelope()
        payloads = {}
        for file in envelope["files"]:
            payload = (file["role"] + " fixture bytes").encode()
            file.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            payloads[file["path"]] = payload
        body = ("import os\nfrom pathlib import Path\n"
                "root=Path(os.environ['HOME'])/'export'\nroot.mkdir(mode=0o700)\n"
                f"payloads={payloads!r}\n"
                "for name,payload in payloads.items():\n"
                "    path=root/name\n    parent=root\n"
                "    for part in Path(name).parts[:-1]:\n        parent=parent/part\n        parent.mkdir(mode=0o700,exist_ok=True)\n"
                "    path.write_bytes(payload)\n    path.chmod(0o600)\n"
                f"path=root/{grammar.CI_ENVELOPE_NAME!r}\n"
                f"path.write_bytes({canonical_json(envelope)!r})\npath.chmod(0o600)\n"
                "print('candidate export complete',flush=True)\n")
        result = self.run_dispatcher(body)
        self.assertEqual(result.returncode, 0)
        self.assert_quiescent()
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c",
                self.candidate_freeze_program(body, result) + "freeze()\n", cwd=self.root)
        frozen = Path(str(BUILD_VALIDATION_ROOT))
        self.assertEqual(verify_build_export(frozen, plan=ci_plan()), envelope)
        for leaf in (frozen, *frozen.rglob("*")):
            self.assertEqual((leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)),
                             (os.getuid(), os.getgid(), 0o700 if leaf.is_dir() else 0o600))
        candidate = authenticate_worker_account("candidate")
        for file in envelope["files"]:
            original = Path(candidate.home) / "export" / file["path"]
            info = command("/usr/bin/sudo", "-n", "/usr/bin/stat", "-c", "%u:%g:%i", str(original)).stdout.decode().strip().split(":")
            self.assertEqual(tuple(map(int, info[:2])), (candidate.uid, candidate.gid))
            self.assertNotEqual(int(info[2]), (frozen / file["path"]).stat().st_ino)
        for role in WORKER_ACCOUNTS:
            account = authenticate_worker_account(role)
            prefix = ("/usr/bin/sudo", "-n", "--", "/usr/bin/setpriv", "--no-new-privs",
                      f"--reuid={account.uid}", f"--regid={account.gid}", "--clear-groups", "--",
                      "/usr/bin/env", "-i", f"--chdir={account.home}", "PATH=/usr/bin:/bin")
            self.assertEqual(command(*prefix, "/usr/bin/id", "-u", cwd=self.root).stdout.strip(), str(account.uid).encode())
            for flag in ("-r", "-w"):
                self.assertEqual(command(*prefix, "/usr/bin/test", flag, str(frozen), accepted=(0, 1), cwd=self.root).returncode, 1)

    def policy_dispatch(self, *, include_pass):
        """Benign reviewed runner/source fixture, not production overlay/import provenance."""
        from mod_base.model import limits
        candidate = authenticate_worker_account("candidate")
        checkout = Path(__file__).resolve().parents[1]
        paths = ("tools/parallel_unittest.py", "src/mod_base/__init__.py", "src/mod_base/errors.py",
                 "src/mod_base/model/__init__.py", "src/mod_base/model/limits.py",
                 "src/mod_base/build_ci/__init__.py", "src/mod_base/build_ci/policy.py")
        code = self.root / "policy-code"
        code.mkdir(mode=0o700)
        for name in paths:
            target = code / name
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.write_bytes((checkout / name).read_bytes())
        source = checkout / "src"
        grant = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
                 "from pathlib import Path\n"
                 "from mod_base.io.tree import grant_source_read_access\n"
                 f"grant_source_read_access(Path({str(code)!r}),tracked_paths={tuple(sorted(paths))!r},"
                 f"source_owner_uid={os.getuid()},owner_uid={os.getuid()},reader_gid={candidate.gid},"
                 f"max_files={limits.MAX_CI_ADAPTER_FILES},max_entries={limits.MAX_CI_SOURCE_ENTRIES},"
                 f"max_total_bytes={limits.MAX_CI_ADAPTER_TREE_BYTES},max_file_bytes={limits.MAX_CI_ADAPTER_FILE_BYTES})\n")
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c", grant, cwd=self.root)
        self.assertEqual(self.as_worker("candidate", "/usr/bin/test", "-w", str(code), accepted=(0,1)).returncode,1)
        fixture = ("import os,tempfile,unittest\n"
                   "class Missing(unittest.TestCase):\n"
                   "    @classmethod\n    def setUpClass(cls): raise unittest.SkipTest('inert fixture skip')\n"
                   "    def test_a(self): raise AssertionError('must not run')\n"
                   "    def test_b(self): raise AssertionError('must not run')\n")
        if include_pass:
            fixture += ("class Available(unittest.TestCase):\n    def test_uid(self):\n"
                        f"        self.assertEqual(os.getuid(),{candidate.uid})\n"
                        "        self.assertNotIn('GITHUB_TOKEN',os.environ)\n"
                        "        self.assertNotIn('GITHUB_ENV',os.environ)\n"
                        "        self.assertTrue(os.path.basename(tempfile.gettempdir()).startswith('worker-'))\n")
        body = ("import runpy,sys\nfrom pathlib import Path\n"
                "start=Path('scripts/ci/policy-fixture')\nstart.mkdir(mode=0o700)\n"
                f"(start/'test_policy_fixture.py').write_text({fixture!r},encoding='utf-8')\n"
                f"sys.path.insert(0,{str(code / 'src')!r})\n"
                f"sys.argv=[{str(code / 'tools/parallel_unittest.py')!r},str(start),"
                "'--policy-profile','quick-skin','-j','2','--slowest','0']\n"
                f"runpy.run_path({str(code / 'tools/parallel_unittest.py')!r},run_name='__main__')\n")
        return self.run_dispatcher(body)

    def test_policy_runner_class_skip_and_real_candidate_uid_finish_quiescent(self):
        result = self.policy_dispatch(include_pass=True)
        self.assertEqual(result.returncode,0)
        self.assertIn(b"1 of 3 discovered tests ran",result.log)
        self.assertIn(b"OK (skipped=1)",result.log)
        self.assert_quiescent()

    def test_policy_runner_all_class_skips_cannot_return_success(self):
        with self.assertRaises(WorkerExecutionError) as caught:
            self.policy_dispatch(include_pass=False)
        self.assertEqual(caught.exception.result.returncode,1)
        self.assertIn(b"no test ran",caught.exception.result.log)
        self.assert_quiescent()

    def test_successful_candidate_that_changes_tracked_source_cannot_freeze(self):
        body = "from pathlib import Path\nPath(__file__).write_text('mutated tracked source',encoding='utf-8')\n"
        result = self.run_dispatcher(body)
        self.assertEqual(result.returncode, 0)
        program = (self.candidate_freeze_program(body, result)
                   + "try:\n    freeze()\nexcept SourceError:\n    print('tracked mutation refused',flush=True)\n"
                     "else:\n    raise AssertionError('tracked mutation admitted')\n")
        observed = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-c", program, cwd=self.root)
        self.assertEqual(observed.stdout.strip(), b"tracked mutation refused")
        self.assertFalse(Path(str(BUILD_VALIDATION_ROOT)).exists())
        self.assert_quiescent()

    def test_dispatcher_that_leaves_an_orphan_holding_stdout_fails_and_the_orphan_is_reaped(self):
        # The contract fails a hook that leaves a process behind, even when the hook itself succeeded.
        started = time.monotonic()
        with self.assertRaisesRegex(WorkerExecutionError, "left a process behind") as caught:
            self.run_dispatcher(
                "import os,time\n"
                "if os.fork() == 0:\n    time.sleep(300)\n    os._exit(0)\n"
                "print('native-success', flush=True)\n")
        self.assertEqual(caught.exception.result.returncode, 0)
        self.assertIn(b"native-success", caught.exception.result.log)
        self.assertLess(time.monotonic() - started, 30)  # The orphan was killed, not waited for.
        self.assert_quiescent()

    def test_children_a_dispatcher_waited_for_or_only_left_as_zombies_are_no_orphans(self):
        result = self.run_dispatcher(
            "import os,subprocess,sys\n"
            "subprocess.run([sys.executable,'-I','-c','print(1)'],check=True)\n"
            "for _ in range(20):\n    subprocess.Popen(['/usr/bin/true'])\n"  # Never waited for.
            "print('native-success', flush=True)\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"native-success", result.log)
        self.assert_quiescent()

    def test_dispatcher_starts_with_a_private_umask_whatever_the_login_default_is(self):
        result = self.run_dispatcher(
            "import os,sys\nfrom pathlib import Path\n"
            "mask=os.umask(0)\nos.umask(mask)\n"
            "home=Path(os.environ['HOME'])\n(home/'made').mkdir()\n(home/'made'/'leaf').write_bytes(b'x')\n"
            "print('umask %03o dir %03o file %03o argv %s' % (mask, (home/'made').stat().st_mode & 0o777,"
            " (home/'made'/'leaf').stat().st_mode & 0o777, sys.argv[1:]), flush=True)\n", extra=("--hook", "policy"))
        self.assertEqual(result.log.strip(), b"umask 077 dir 700 file 600 argv ['--hook', 'policy']")
        self.assert_quiescent()

    def test_failed_execution_never_returns_success_and_reaps_its_uid(self):
        with self.assertRaises(WorkerExecutionError) as caught:
            self.run_dispatcher("import sys\nprint('native-failure', flush=True)\nsys.exit(17)\n")
        self.assertEqual(caught.exception.result.returncode, 17)
        self.assertIn(b"native-failure", caught.exception.result.log)
        self.assert_quiescent()

    def test_timeout_reaps_parent_and_child_and_retains_bounded_log(self):
        with self.assertRaisesRegex(WorkerExecutionError, "timed out") as caught:
            self.run_dispatcher("import os,time\nprint('before-timeout', flush=True)\nos.fork()\ntime.sleep(300)\n", timeout=1)
        self.assertIn(b"before-timeout", caught.exception.result.log)
        self.assert_quiescent()

    def test_cancellation_signal_still_kills_and_locks_the_uid(self):
        account = authenticate_worker_account("candidate")
        errors = []
        def cancel_when_started():
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if command("/usr/bin/pgrep", "-u", str(account.uid), accepted=(0, 1)).stdout:
                        import signal
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                    time.sleep(0.05)
                raise AssertionError("cancellation fixture did not start")
            except Exception as error:
                errors.append(error)
        cancel = threading.Thread(target=cancel_when_started, daemon=True)
        cancel.start()
        try:
            with self.assertRaisesRegex(WorkerExecutionError, "interrupted"):
                self.run_dispatcher("import os,time\nos.fork()\ntime.sleep(300)\n", timeout=15)
        finally:
            cancel.join(timeout=20)
        self.assertFalse(cancel.is_alive())
        self.assertEqual(errors, [])
        self.assert_quiescent()

    def test_second_account_uses_its_protected_controller_dispatcher(self):
        result = self.run_dispatcher("import os\nprint(os.getuid(), flush=True)\n", role="validator")
        self.assertEqual(result.log.strip(), str(authenticate_worker_account("validator").uid).encode())
        self.assertEqual(result.returncode, 0)
        self.assert_quiescent("validator")

    def test_noisy_dispatcher_is_drained_after_the_capture_limit(self):
        result = self.run_dispatcher("import sys\nsys.stdout.buffer.write(b'x' * "
                                     f"{limits.MAX_CI_LOG_BYTES + limits.CI_PROCESS_READ_BYTES})\n"
                                     "sys.stdout.buffer.flush()\n")
        self.assertEqual(len(result.log), limits.MAX_CI_LOG_BYTES)
        self.assertTrue(result.truncated)
        self.assertEqual(result.returncode, 0)
        self.assert_quiescent()


class LinuxSourceTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" \
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("source integration tests require GitHub-hosted Linux")
        directory = tempfile.TemporaryDirectory(prefix="mod-base-source-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "empty").write_bytes(b"")
        (self.root / "run").write_bytes(b"abc")
        (self.root / "run").chmod(0o755)
        (self.root / "link").symlink_to("../../outside")
        listing = b""
        for path, mode, data in [("empty", "100644", b""), ("link", "120000", b"../../outside"),
                                 ("run", "100755", b"abc")]:
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            listing += f"{mode} blob {blob} {len(data)}\t{path}\0".encode()
        self.inventory = parse_source_inventory(listing)

    def test_atomic_copy_preserves_git_identity_and_omits_metadata(self):
        (self.root / ".git").symlink_to("/etc")
        with tempfile.TemporaryDirectory(prefix="mod-base-destination-") as directory:
            output = Path(directory) / "source"
            records = materialize_source_copy(self.root, output, inventory=self.inventory)
            self.assertEqual(records, verify_source_copy(output, inventory=self.inventory))
            self.assertFalse((output / ".git").exists())
            self.assertEqual(os.readlink(output / "link"), "../../outside")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "run").stat().st_mode), 0o755)
            self.assertEqual((output / "empty").read_bytes(), b"")
            with self.assertRaises(MbError):
                materialize_source_copy(self.root, output, inventory=self.inventory)
            self.assertEqual(records, verify_source_copy(output, inventory=self.inventory))

    def test_source_change_during_staging_leaves_no_output_or_stage(self):
        def changing_copy(*args, **kwargs):
            (self.root / "run").write_bytes(b"abd")
            return copy_source_files(*args, **kwargs)
        with tempfile.TemporaryDirectory(prefix="mod-base-destination-") as directory:
            output = Path(directory) / "source"
            with patch("mod_base.build_ci.source.copy_source_files", side_effect=changing_copy), \
                    self.assertRaises(MbError):
                materialize_source_copy(self.root, output, inventory=self.inventory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_preserve_empty_executable_and_literal_link_with_generated_parent(self):
        generated = self.root / "module/versions/target/build"
        generated.mkdir(parents=True)
        (generated / "ignored").symlink_to("/etc/passwd")
        (self.root / ".git").symlink_to("/does-not-exist")
        records = verify_source_copy(self.root, inventory=self.inventory,
                                     generated_roots=("module/versions/target/build",))
        self.assertEqual([record["mode"] for record in records], ["100644", "120000", "100755"])

    def test_changed_bytes_mode_link_and_undeclared_file_fail(self):
        for mutation, restore in [
                (lambda: (self.root / "run").write_bytes(b"abd"), lambda: (self.root / "run").write_bytes(b"abc")),
                (lambda: (self.root / "run").chmod(0o644), lambda: (self.root / "run").chmod(0o755)),
                (lambda: (self.root / "extra").write_bytes(b""), lambda: (self.root / "extra").unlink())]:
            mutation()
            try:
                with self.assertRaises(MbError):
                    verify_source_copy(self.root, inventory=self.inventory)
            finally:
                restore()
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("../../changed")
        with self.assertRaises(MbError):
            verify_source_copy(self.root, inventory=self.inventory)

    def test_tracked_leaf_in_generated_root_still_verified_and_hardlink_rejected(self):
        os.link(self.root / "run", self.root / "alias")
        with self.assertRaises(MbError):
            verify_source_copy(self.root, inventory=self.inventory)
        (self.root / "alias").unlink()
        generated = self.root / "build"
        generated.mkdir()
        (generated / "tracked").write_bytes(b"bad")
        expected = parse_source_inventory(b"100644 blob " + b"a" * 40 + b" 3\tbuild/tracked\0")
        with self.assertRaises(MbError):
            verify_source_copy(self.root, inventory=expected, generated_roots=("build",))

    def test_byte_budget_rejected_before_read_and_generated_symlink_rejected(self):
        with self.assertRaises(MbError):
            source_records(self.root, tracked_paths=("run",), generated_roots=(), max_files=10,
                           max_entries=20, max_total_bytes=2, max_file_bytes=10, max_link_bytes=10)
        (self.root / "build").symlink_to("/tmp")
        with self.assertRaises(MbError):
            verify_source_copy(self.root, inventory=self.inventory, generated_roots=("build",))


class LinuxControllerSourceTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real controller-copy tests require Linux")
        from tests.test_ci_controller import ControllerSourceTests
        from mod_base.build_ci.controller import authenticate_controller_sources
        self.plan, api, _, protected = ControllerSourceTests().fixture(
            alter=lambda config, rows: rows[0].update(mode="100755"))
        self.sources = authenticate_controller_sources(api, identity=self.plan["identity"],
                                                       protected_paths=protected)
        directory = tempfile.TemporaryDirectory(prefix="mod-base-controller-copy-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for file in (self.sources.config, *self.sources.files):
            path = self.root / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file.data)
            path.chmod(0o755 if file.mode == "100755" else 0o644)

    def verify(self):
        from mod_base.build_ci.controller import verify_controller_source_copy
        return verify_controller_source_copy(self.root, sources=self.sources,
                                             identity=self.plan["identity"])

    def test_exact_inert_copy_and_changed_bytes_or_mode(self):
        self.assertEqual(self.verify()["repository"], self.plan["identity"]["repository"])
        file = self.sources.files[0]
        path = self.root / file.path
        path.write_bytes(b"changed")
        with self.assertRaises(MbError):
            self.verify()
        path.write_bytes(file.data)
        path.chmod(0o644 if file.mode == "100755" else 0o755)
        with self.assertRaises(MbError):
            self.verify()

    def test_undeclared_imports_git_metadata_links_and_hardlinks(self):
        for kind in ("module", "git", "symlink", "hardlink"):
            extra = self.root / (".git" if kind == "git" else "undeclared.py")
            if kind == "symlink":
                extra.symlink_to(self.root / self.sources.files[0].path)
            elif kind == "hardlink":
                os.link(self.root / self.sources.files[0].path, extra)
            else:
                extra.write_bytes(b"raise AssertionError('never import')\n")
            try:
                with self.subTest(kind=kind), self.assertRaises(MbError):
                    self.verify()
            finally:
                extra.unlink()

    def test_atomic_materialization_preserves_bytes_modes_and_excludes_existing_outputs(self):
        from mod_base.build_ci.controller import materialize_controller_sources, verify_controller_source_copy
        output = self.root / "sealed"
        expected = materialize_controller_sources(output, sources=self.sources, identity=self.plan["identity"])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(expected, verify_controller_source_copy(output, sources=self.sources,
                                                                identity=self.plan["identity"]))
        for file in (self.sources.config, *self.sources.files):
            observed = output / file.path
            self.assertEqual(observed.read_bytes(), file.data)
            self.assertEqual(observed.stat().st_nlink, 1)
            self.assertEqual(stat.S_IMODE(observed.stat().st_mode), 0o755 if file.mode == "100755" else 0o644)
        with self.assertRaises(MbError):
            materialize_controller_sources(output, sources=self.sources, identity=self.plan["identity"])
        self.assertEqual(expected, verify_controller_source_copy(output, sources=self.sources,
                                                                identity=self.plan["identity"]))

    def test_materialization_rejects_changed_stage_without_publishing_or_leaking_stage(self):
        from mod_base.build_ci import controller
        output = self.root / "sealed"
        original = controller._write_controller_files
        before = set(self.root.iterdir())
        def changing(stage_fd, files):
            original(stage_fd, files)
            descriptor = os.open("undeclared.py", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=stage_fd)
            os.close(descriptor)
        with patch.object(controller, "_write_controller_files", side_effect=changing), self.assertRaises(MbError):
            controller.materialize_controller_sources(output, sources=self.sources, identity=self.plan["identity"])
        self.assertFalse(output.exists())
        self.assertEqual(set(self.root.iterdir()), before)


class LinuxValidationPlanTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real protected plan input tests require Linux")
        directory = tempfile.TemporaryDirectory(prefix="mod-base-validation-plan-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.output = self.base / "input"

    def test_exact_private_plan_copy_and_existing_output_preservation(self):
        from mod_base.build_ci.inputs import materialize_validation_inputs, verify_validation_plan
        plan = ci_plan()
        materialize_validation_inputs(self.output, sources=SOURCES, plan=plan)
        self.assertEqual(verify_validation_plan(self.output, plan=plan), plan)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()},
                         {grammar.CI_PLAN_NAME: canonical_json(plan), **SOURCES})
        with self.assertRaises(MbError):
            materialize_validation_inputs(self.output, sources=SOURCES, plan=plan)
        self.assertEqual(verify_validation_plan(self.output, plan=plan), plan)

    def test_changed_plan_links_and_undeclared_import_files_are_refused(self):
        from mod_base.build_ci.inputs import materialize_validation_inputs, verify_validation_plan
        plan = ci_plan()
        materialize_validation_inputs(self.output, sources=SOURCES, plan=plan)
        for name in SOURCES:  # A candidate file with other bytes than the plan binds.
            original = (self.output / name).read_bytes()
            (self.output / name).write_bytes(original + b" ")
            with self.subTest(changed=name), self.assertRaises(MbError):
                verify_validation_plan(self.output, plan=plan)
            (self.output / name).write_bytes(original)
        leaf = self.output / grammar.CI_PLAN_NAME
        leaf.write_bytes(b" " + canonical_json(plan))
        with self.assertRaises(MbError):
            verify_validation_plan(self.output, plan=plan)
        leaf.write_bytes(canonical_json(plan))
        for kind in ("symlink", "hardlink", "fifo", "pth"):
            extra = self.output / "extra.pth"
            if kind == "symlink":
                extra.symlink_to("/etc/passwd")
            elif kind == "hardlink":
                os.link(leaf, extra)
            elif kind == "fifo":
                os.mkfifo(extra)
            else:
                extra.write_bytes(b"inert undeclared import path\n")
            try:
                with self.subTest(kind=kind), self.assertRaises(MbError):
                    verify_validation_plan(self.output, plan=plan)
            finally:
                extra.unlink()

    def test_failed_independent_stage_verification_leaves_no_output(self):
        from mod_base.build_ci.inputs import materialize_validation_inputs
        with patch("mod_base.build_ci.inputs.verify_validation_inputs", side_effect=MbError("stage mismatch")), \
                self.assertRaises(MbError):
            materialize_validation_inputs(self.output, sources=SOURCES, plan=ci_plan())
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.base.iterdir()), [])


class LinuxValidationExportTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real verifier-output tests require Linux")
        from tests.helpers import ci_validation
        directory = tempfile.TemporaryDirectory(prefix="mod-base-validation-copy-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "reclaimed"
        self.root.mkdir(mode=0o700)
        # What a verifier leaves is its reports (`<unit id>.json`); the record is built when they are sealed.
        self.receipt = ci_validation()
        self.context = {"plan": ci_plan(), **{key: self.receipt[key] for key in
                        ("hook", "unit_id", "run_id", "run_attempt", "source_config_sha256", "input_sha256")}}
        for report in self.receipt["reports"]:
            report["path"] = report["unit_id"] + ".json"
            (self.root / report["path"]).write_bytes(canonical_json({"fixture_unit": report["unit_id"]}))
        self.output = self.base / "sealed"

    def test_copy_has_independent_inodes_exact_bytes_and_existing_output_is_preserved(self):
        from mod_base.build_ci.validation import materialize_validation_export, verify_validation_export
        self.assertEqual(materialize_validation_export(self.root, self.output, **self.context), self.receipt)
        self.assertEqual((self.output / grammar.CI_VALIDATION_NAME).read_bytes(), canonical_json(self.receipt))
        self.assertFalse((self.root / grammar.CI_VALIDATION_NAME).exists())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        with self.assertRaises(MbError):
            materialize_validation_export(self.root, self.output, **self.context)
        for report in self.receipt["reports"]:
            original, copied = self.root / report["path"], self.output / report["path"]
            self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
            self.assertEqual(copied.stat().st_nlink, 1)
            original.write_bytes(b"changed after copy")
        self.assertEqual(verify_validation_export(self.output, **self.context), self.receipt)

    def test_symlink_hardlink_and_special_entries_never_publish(self):
        from mod_base.build_ci.validation import materialize_validation_export
        for kind in ("symlink", "hardlink", "fifo"):
            extra = self.root / "extra.json"
            if kind == "symlink":
                extra.symlink_to("/etc/passwd")
            elif kind == "hardlink":
                os.link(self.root / self.receipt["reports"][0]["path"], extra)
            else:
                os.mkfifo(extra)
            try:
                with self.subTest(kind=kind), self.assertRaises(MbError):
                    materialize_validation_export(self.root, self.output, **self.context)
                self.assertFalse(self.output.exists())
            finally:
                extra.unlink()

    def test_hash_matching_duplicate_native_json_is_rejected(self):
        from mod_base.build_ci.validation import materialize_validation_export, verify_validation_export
        data = b'{"observed":1,"observed":2}\n'
        report = self.receipt["reports"][0]
        (self.root / report["path"]).write_bytes(data)
        with self.assertRaises(MbError):  # No record is built from it,
            materialize_validation_export(self.root, self.output, **self.context)
        self.assertFalse(self.output.exists())
        report.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.root / grammar.CI_VALIDATION_NAME).write_bytes(canonical_json(self.receipt))
        with self.assertRaises(MbError):  # and a record that names its hash is refused by a reader.
            verify_validation_export(self.root, **self.context)

    def test_changed_private_stage_leaves_no_output_or_leaked_stage(self):
        from mod_base.build_ci import validation
        original = validation.copy_regular_files
        def changing(root, stage_fd, **kwargs):
            records = original(root, stage_fd, **kwargs)
            descriptor = os.open(self.receipt["reports"][0]["path"], os.O_WRONLY | os.O_TRUNC,
                                 dir_fd=stage_fd)
            try:
                os.write(descriptor, b"changed private report")
            finally:
                os.close(descriptor)
            return records
        with patch.object(validation, "copy_regular_files", side_effect=changing), self.assertRaises(MbError):
            validation.materialize_validation_export(self.root, self.output, **self.context)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.base.iterdir()), [self.root])


class LinuxExportCopyTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real export-copy tests require Linux")
        directory = tempfile.TemporaryDirectory(prefix="mod-base-export-copy-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "reclaimed"
        self.root.mkdir(mode=0o700)
        self.plan = ci_plan()
        self.envelope = ci_envelope()
        for record in self.envelope["files"]:
            path = self.root / record["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (record["role"] + " fixture bytes").encode()
            path.write_bytes(data)
            record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(self.envelope))
        self.output = self.base / "sealed"

    def test_atomic_copy_has_independent_inodes_private_root_and_exact_verified_bytes(self):
        result = materialize_build_export(self.root, self.output, plan=self.plan)
        self.assertEqual(result, self.envelope)
        self.assertEqual(result, verify_build_export(self.output, plan=self.plan))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        with self.assertRaises(MbError):
            materialize_build_export(self.root, self.output, plan=self.plan)
        for record in self.envelope["files"]:
            original, sealed = self.root / record["path"], self.output / record["path"]
            self.assertNotEqual(original.stat().st_ino, sealed.stat().st_ino)
            self.assertEqual(sealed.stat().st_nlink, 1)
            original.write_bytes(b"mutated after copy")
        self.assertEqual(result, verify_build_export(self.output, plan=self.plan))
        self.assertEqual(result, verify_build_export(self.output, plan=self.plan))

    def test_link_hardlink_and_special_source_never_publish(self):
        for kind in ("link", "hardlink", "fifo"):
            extra = self.root / "hostile"
            if kind == "link":
                extra.symlink_to("/etc/passwd")
            elif kind == "hardlink":
                os.link(self.root / self.envelope["files"][0]["path"], extra)
            else:
                os.mkfifo(extra)
            try:
                with self.subTest(kind=kind), self.assertRaises(MbError):
                    materialize_build_export(self.root, self.output, plan=self.plan)
                self.assertFalse(self.output.exists())
            finally:
                extra.unlink()

    def test_mutation_during_copy_leaves_no_published_output_or_stage(self):
        def changing_copy(*args, **kwargs):
            (self.root / self.envelope["files"][0]["path"]).write_bytes(b"changed before copying")
            return copy_regular_files(*args, **kwargs)
        with patch("mod_base.build_ci.exports.copy_regular_files", side_effect=changing_copy), self.assertRaises(MbError):
            materialize_build_export(self.root, self.output, plan=self.plan)
        self.assertEqual(list(self.base.iterdir()), [self.root])

    def test_entry_budget_is_enforced_before_any_content_inventory(self):
        stage = self.base / "stage"
        stage.mkdir(mode=0o700)
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with patch("mod_base.io.tree.file_records") as content, self.assertRaises(MbError):
                copy_regular_files(self.root, descriptor, max_files=100, max_entries=1,
                                   max_total_bytes=100000, max_file_bytes=10000)
            content.assert_not_called()
            self.assertEqual(list(stage.iterdir()), [])
        finally:
            os.close(descriptor)


class LinuxBuildTransportTests(unittest.TestCase):
    def test_real_complete_target_set_is_one_private_publication(self):
        if sys.platform != "linux":
            raise AssertionError("real target-set transport tests require Linux")
        from tests.test_ci_transport import target_set_world
        from mod_base.build_ci.transport import download_target_set
        world = target_set_world()
        with tempfile.TemporaryDirectory(prefix="mod-base-target-set-") as directory:
            output = Path(directory) / "inputs"
            self.assertEqual(download_target_set(world.api, descriptors=world.descriptors, plan=world.plan,
                                                 run_id=42, run_attempt=2, output=output), world.partitions)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["target-0", "target-1"])
            for index, partition in enumerate(world.partitions):
                self.assertEqual(verify_build_export(output / f"target-{index}", plan=world.plan),
                                 partition["envelope"])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_real_late_target_zip_failure_leaves_no_partial_inputs_or_stage(self):
        if sys.platform != "linux":
            raise AssertionError("real target-set transport tests require Linux")
        from tests.test_ci_transport import target_set_world
        from mod_base.build_ci.transport import download_target_set
        world = target_set_world()
        # The second archive is what its descriptor and the API say it is, so it is rejected by the
        # extractor after the first target was really written into the stage.
        selected = world.descriptors[1]["artifact"]
        data = b"not a ZIP"
        selected.update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
        world.archives[selected["id"]] = data
        world.set_artifact(selected["id"], size_in_bytes=len(data), digest=selected["digest"])
        with tempfile.TemporaryDirectory(prefix="mod-base-target-set-") as directory:
            with self.assertRaises(MbError):
                download_target_set(world.api, descriptors=world.descriptors, plan=world.plan,
                                    run_id=42, run_attempt=2, output=Path(directory) / "inputs")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_real_same_attempt_target_download_before_aggregate_completion(self):
        if sys.platform != "linux":
            raise AssertionError("real target transport tests require Linux")
        from tests.helpers import ci_run_descriptor
        from tests.test_ci_transport import ASSEMBLE, GATE, World, build_archive
        from mod_base.build_ci.transport import download_target_set
        # One target is read as a set of one: its job has sealed and uploaded, the assembling job of
        # the same attempt is the reader and still runs, and the run has no conclusion yet.
        world = World()
        world.add_run("build", "build-full", status="in_progress", conclusion=None)
        jobs = [job for job in world.jobs[42] if job["name"] != GATE]
        assemble = next(job for job in jobs if job["name"] == ASSEMBLE)
        assemble.update(status="in_progress", conclusion=None, completed_at=None)
        assemble["steps"] = assemble["steps"][:1]
        world.set_jobs(42, jobs)
        producer = ci_run_descriptor(world.plan, "build", "full", "target", unit_id="target-a")["producer"]
        data, envelope = build_archive(world.plan, producer, target_id="target-a")
        descriptor = world.publish(world.describe("build", "full", "target", data, unit_id="target-a",
                                                  artifact_id=110), data)
        with tempfile.TemporaryDirectory(prefix="mod-base-target-download-") as directory:
            output = Path(directory) / "output"
            self.assertEqual(download_target_set(world.api, descriptors=[descriptor], plan=world.plan,
                                                 run_id=42, run_attempt=2, output=output),
                             [{"descriptor": descriptor, "envelope": envelope}])
            self.assertEqual([path.name for path in output.iterdir()], ["target-0"])
            self.assertEqual(verify_build_export(output / "target-0", plan=world.plan), envelope)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_real_numeric_download_extract_verify_and_independent_private_copy(self):
        if sys.platform != "linux":
            raise AssertionError("real Build transport tests require Linux")
        from tests.test_ci_transport import transport_fixture
        from mod_base.build_ci.transport import download_completed_build
        plan, api, descriptor, envelope, *_ = transport_fixture()
        with tempfile.TemporaryDirectory(prefix="mod-base-build-download-") as directory:
            base = Path(directory)
            output = base / "output"
            observed = download_completed_build(api, descriptor=descriptor, plan=plan, output=output)
            self.assertEqual(observed, envelope)
            self.assertEqual(verify_build_export(output, plan=plan), envelope)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(list(base.iterdir()), [output])
            self.assertEqual(api.mutations, [])

    def test_real_corrupt_inventory_rejects_without_output_or_temporary_residue(self):
        if sys.platform != "linux":
            raise AssertionError("real Build transport tests require Linux")
        import io
        import zipfile
        from tests.test_ci_transport import transport_fixture
        from mod_base.build_ci.transport import download_completed_build
        plan, api, descriptor, _, _, record, data = transport_fixture()
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(stream, "w") as changed:
            for name in original.namelist():
                changed.writestr(name, original.read(name) if name == "ci-envelope.json" else b"corrupt")
        data = stream.getvalue()
        descriptor["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
        record.update(size_in_bytes=len(data), digest=descriptor["artifact"]["digest"])
        api.add_artifact(record, data)
        with tempfile.TemporaryDirectory(prefix="mod-base-build-download-") as directory:
            output = Path(directory) / "output"
            with self.assertRaises(MbError):
                download_completed_build(api, descriptor=descriptor, plan=plan, output=output)
            self.assertEqual(list(Path(directory).iterdir()), [])


class LinuxBuildAssemblyTests(unittest.TestCase):
    def inputs(self, directory):
        if sys.platform != "linux":
            raise AssertionError("real complete Build assembly requires Linux")
        from tests.test_ci_transport import target_set_world
        from mod_base.build_ci.transport import download_target_set
        world = target_set_world()
        root = Path(directory) / "inputs"
        partitions = download_target_set(world.api, descriptors=world.descriptors, plan=world.plan,
                                         run_id=42, run_attempt=2, output=root)
        return world.plan, partitions, root

    def test_real_transport_and_complete_independent_byte_assembly(self):
        from mod_base.build_ci.exports import assemble_build_export
        with tempfile.TemporaryDirectory(prefix="mod-base-build-assembly-") as directory:
            plan, partitions, inputs = self.inputs(directory)
            output = Path(directory) / "output"
            envelope = assemble_build_export(inputs, partitions=partitions, plan=plan,
                                             run_id=42, run_attempt=2, output=output)
            self.assertEqual((envelope["scope"], envelope["target_id"]), ("complete", None))
            self.assertEqual(verify_build_export(output, plan=plan), envelope)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(set(Path(directory).iterdir()), {inputs, output})
            self.assertNotIn("upload_window", envelope["producer"])
            for index, partition in enumerate(partitions):
                source = inputs / f"target-{index}"
                self.assertEqual(verify_build_export(source, plan=plan), partition["envelope"])
                for file in partition["envelope"]["files"]:
                    original, copied = source / file["path"], output / file["path"]
                    self.assertEqual(original.read_bytes(), copied.read_bytes())
                    self.assertNotEqual((original.stat().st_dev, original.stat().st_ino),
                                        (copied.stat().st_dev, copied.stat().st_ino))
                    self.assertEqual(copied.stat().st_nlink, 1)

    def test_real_corrupt_target_rejects_before_output(self):
        from mod_base.build_ci.exports import assemble_build_export
        with tempfile.TemporaryDirectory(prefix="mod-base-build-assembly-reject-") as directory:
            plan, partitions, inputs = self.inputs(directory)
            (inputs / "target-1" / partitions[1]["envelope"]["files"][0]["path"]).write_bytes(b"corrupt")
            output = Path(directory) / "output"
            with self.assertRaises(MbError):
                assemble_build_export(inputs, partitions=partitions, plan=plan,
                                      run_id=42, run_attempt=2, output=output)
            self.assertEqual(list(Path(directory).iterdir()), [inputs])

    def test_real_late_failure_or_source_mutation_removes_partial_stage(self):
        from mod_base.build_ci import exports
        original = exports.copy_selected_regular_files
        for late_failure in (True, False):
            with self.subTest(late_failure=late_failure), tempfile.TemporaryDirectory(prefix="mod-base-build-assembly-late-") as directory:
                plan, partitions, inputs = self.inputs(directory)
                def copying(root, stage_fd, **kwargs):
                    if late_failure and root.name == "target-1":
                        raise MbError("late target copy rejected")
                    result = original(root, stage_fd, **kwargs)
                    if not late_failure and root.name == "target-0":
                        (root / partitions[0]["envelope"]["files"][0]["path"]).write_bytes(b"late corruption")
                    return result
                with patch.object(exports, "copy_selected_regular_files", side_effect=copying), self.assertRaises(MbError):
                    exports.assemble_build_export(inputs, partitions=partitions, plan=plan,
                        run_id=42, run_attempt=2, output=Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [inputs])

    def test_real_selected_copy_refuses_replacement_and_unsafe_source_or_destination_links(self):
        if sys.platform != "linux":
            raise AssertionError("real selected-copy links require Linux")
        from mod_base.io.tree import copy_selected_regular_files
        for mutation in ("collision", "source-symlink", "source-hardlink", "parent-symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="mod-base-selected-copy-") as directory:
                base = Path(directory)
                source, stage, outside = base / "source", base / "stage", base / "outside"
                for root in (source, stage, outside):
                    root.mkdir(mode=0o700)
                (source / "payload").write_bytes(b"source")
                (outside / "untouched").write_bytes(b"outside")
                path = "payload"
                if mutation == "collision":
                    (stage / path).write_bytes(b"existing")
                elif mutation == "source-symlink":
                    (source / "unselected").symlink_to(outside / "untouched")
                elif mutation == "source-hardlink":
                    os.link(outside / "untouched", source / "unselected")
                else:
                    (source / "nested").mkdir()
                    (source / "payload").rename(source / "nested" / "payload")
                    (stage / "nested").symlink_to(outside, target_is_directory=True)
                    path = "nested/payload"
                descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with self.assertRaises(MbError):
                        copy_selected_regular_files(source, descriptor, paths=(path,),
                            max_files=10, max_entries=20, max_total_bytes=100, max_file_bytes=50)
                finally:
                    os.close(descriptor)
                self.assertEqual((outside / "untouched").read_bytes(), b"outside")
                self.assertFalse((outside / "payload").exists())
                if mutation == "collision":
                    self.assertEqual((stage / "payload").read_bytes(), b"existing")


class LinuxGateTransportTests(unittest.TestCase):
    def test_real_build_and_packaged_record_extraction_and_api_binding(self):
        if sys.platform != "linux":
            raise AssertionError("real tested-record transport requires Linux")
        from tests.test_ci_gate_timeline import gate_world
        from mod_base.build_ci.transport import download_gate_receipt
        for kind in ("build", "packaged"):
            world = gate_world(kind)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="mod-base-gate-download-") as directory:
                observed = download_gate_receipt(world.api, descriptor=world.seals[kind], plan=world.plan,
                                                 gate=kind, temporary_root=Path(directory))
                self.assertEqual(observed, world.documents[kind])
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(world.api.mutations, [])

    def test_real_wrong_filename_and_noncanonical_record_leave_no_residue(self):
        if sys.platform != "linux":
            raise AssertionError("real tested-record transport requires Linux")
        from tests.test_ci_gate_timeline import gate_world
        from tests.test_ci_gate_transport import reseal
        from mod_base.build_ci.transport import download_gate_receipt
        for world in (reseal(gate_world(), "build", filename="other.json"),
                      reseal(gate_world(), "build", raw=canonical_json(gate_world().documents["build"]) + b"\n")):
            with tempfile.TemporaryDirectory(prefix="mod-base-gate-reject-") as directory:
                with self.assertRaises(MbError):
                    download_gate_receipt(world.api, descriptor=world.seals["build"], plan=world.plan,
                                          gate="build", temporary_root=Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])


class LinuxLatestBuildDownloadTests(unittest.TestCase):
    def test_real_newest_selection_download_and_atomic_copy(self):
        if sys.platform != "linux":
            raise AssertionError("real latest Build download requires Linux")
        from tests.test_ci_transport import build_world
        from mod_base.build_ci.selection import download_latest_pr_build
        world = build_world()
        with tempfile.TemporaryDirectory(prefix="mod-base-latest-build-") as directory:
            output = Path(directory) / "output"
            observed = download_latest_pr_build(world.api, plan=world.plan, output=output)
            self.assertEqual(observed, {"descriptor": world.bundle, "envelope": world.envelope})
            for file in world.envelope["files"]:
                self.assertEqual((output / file["path"]).read_bytes(), (file["path"] + "\n").encode())
            self.assertEqual(list(Path(directory).iterdir()), [output])
            self.assertEqual(world.api.mutations, [])

    def test_new_run_after_real_copy_prevents_publication_and_cleans_stage(self):
        if sys.platform != "linux":
            raise AssertionError("real latest Build race requires Linux")
        from tests.test_ci_latest_download import supersede
        from tests.test_ci_transport import build_world
        from mod_base.build_ci import exports
        from mod_base.build_ci.selection import download_latest_pr_build
        world = build_world()
        original = exports.copy_regular_files
        def copying(*args, **kwargs):
            result = original(*args, **kwargs)
            supersede(world)
            return result
        with tempfile.TemporaryDirectory(prefix="mod-base-latest-race-") as directory, \
                patch.object(exports, "copy_regular_files", side_effect=copying):
            with self.assertRaises(MbError):
                download_latest_pr_build(world.api, plan=world.plan, output=Path(directory) / "output")
            self.assertEqual(list(Path(directory).iterdir()), [])


class LinuxBuildArchiveTests(unittest.TestCase):
    def test_real_streamed_stored_zip_and_strict_roundtrip(self):
        if sys.platform != "linux":
            raise AssertionError("real Build archive requires Linux")
        from tests.test_ci_archive import archive_fixture
        from mod_base.build_ci.archive import encode_build_export
        from mod_base.io.bounded_zip import extract_build
        from mod_base.build_ci.exports import verify_build_export
        with tempfile.TemporaryDirectory(prefix="mod-base-encode-") as directory:
            base = Path(directory)
            plan, envelope = archive_fixture(base / "source")
            metadata = encode_build_export(base / "source", base / "encoded", plan=plan)
            data = (base / "encoded" / metadata["path"]).read_bytes()
            self.assertEqual(metadata["size"], len(data))
            self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
            extract_build(base / "encoded" / metadata["path"], base / "decoded")
            self.assertEqual(verify_build_export(base / "decoded", plan=plan), envelope)
            self.assertEqual(list((base / "encoded").iterdir()), [base / "encoded" / metadata["path"]])

    def test_real_compressed_cap_and_late_source_mutation_clean_unpublished_stage(self):
        if sys.platform != "linux":
            raise AssertionError("real Build archive rejection requires Linux")
        from tests.test_ci_archive import archive_fixture
        from mod_base.build_ci import archive
        for mutation in ("cap", "source"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="mod-base-encode-fail-") as directory:
                base = Path(directory)
                plan, envelope = archive_fixture(base / "source")
                original, calls = archive.stream_child_file, [0]
                def streaming(*args, **kwargs):
                    count = original(*args, **kwargs)
                    calls[0] += 1
                    if calls[0] == len(envelope["files"]) + 1:
                        (base / "source" / envelope["files"][0]["path"]).write_bytes(b"changed")
                    return count
                control = patch.object(limits, "MAX_CI_BUNDLE_COMPRESSED_BYTES", 100) if mutation == "cap" else \
                    patch.object(archive, "stream_child_file", side_effect=streaming)
                with control, self.assertRaises(MbError):
                    archive.encode_build_export(base / "source", base / "output", plan=plan)
                self.assertEqual(list(base.iterdir()), [base / "source"])

    def test_real_child_stream_is_chunked_and_refuses_links(self):
        if sys.platform != "linux":
            raise AssertionError("real child streaming requires Linux")
        from mod_base.io.tree import stream_child_file
        with tempfile.TemporaryDirectory(prefix="mod-base-stream-") as directory:
            root = Path(directory)
            data = b"x" * (128 * 1024 + 1)
            (root / "payload").write_bytes(data)
            chunks = []
            self.assertEqual(stream_child_file(root, "payload", max_bytes=len(data), consume=chunks.append), len(data))
            self.assertEqual(b"".join(chunks), data)
            self.assertLessEqual(max(map(len, chunks)), 64 * 1024)
            linked = [False]
            def linking(chunk):
                if not linked[0]:
                    os.link(root / "payload", root / "during")
                    linked[0] = True
            with self.assertRaises(MbError):
                stream_child_file(root, "payload", max_bytes=len(data), consume=linking)
            (root / "during").unlink()
            (root / "link").symlink_to(root / "payload")
            with self.assertRaises(MbError):
                stream_child_file(root, "link", max_bytes=len(data), consume=lambda _: self.fail("link read"))
            os.link(root / "payload", root / "hard")
            with self.assertRaises(MbError):
                stream_child_file(root, "payload", max_bytes=len(data), consume=lambda _: self.fail("hard link read"))


#: What a checkout step leaves in the environment of the commands below; no user or system Git
#: configuration takes part, and the dates make the fixture's commits the same on every run.
STAGING_GIT_ENV = {**HOST_ENV, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Fixture",
                   "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture",
                   "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                   "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z", "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z"}
STAGING_REPOSITORY = "owner/project"
#: Run as the candidate inside its repository: every path outside ``.git`` and the kit overlay with
#: its type, mode, owner, link count and bytes.
STAGING_LISTING = r"""
import json, os, stat, sys
os.chdir(sys.argv[1])
found = {}
for directory, names, files in os.walk("."):
    names[:] = sorted(name for name in names
                      if os.path.normpath(os.path.join(directory, name)) not in (".git", "out/mod-base-kit"))
    for name in (*names, *files):
        path = os.path.normpath(os.path.join(directory, name))
        info = os.lstat(path)
        row = {"mode": stat.S_IMODE(info.st_mode), "owner": [info.st_uid, info.st_gid], "links": info.st_nlink,
               "inode": info.st_ino}
        if stat.S_ISLNK(info.st_mode):
            row.update(kind="link", target=os.readlink(path), mode=None, links=None)
        elif stat.S_ISREG(info.st_mode):
            with open(path, "rb") as stream:
                row.update(kind="file", hex=stream.read().hex())
        else:
            row.update(kind="directory" if stat.S_ISDIR(info.st_mode) else "special", links=None)
        found[path] = row
print(json.dumps(found, sort_keys=True))
"""
#: Run as the candidate: the staged kit must be a complete overlay by the kit's own rules.
STAGING_KIT_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1] + "/src")
from pathlib import Path
from mod_base import pin
root = Path(sys.argv[1])
stamp = pin.read_stamp(root)
assert pin.kit_tree_digest(root) == stamp["tree_digest"]
pin.verify_staged_files(root)
print(json.dumps(stamp, sort_keys=True))
"""


def staging_git(*args, cwd):
    result = subprocess.run(("/usr/bin/git", *args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60, env=STAGING_GIT_ENV, check=False, cwd=cwd)
    if result.returncode != 0:
        raise AssertionError(f"Git fixture command {args[0]} failed: {result.stderr.decode(errors='replace')[-600:]}")
    return result.stdout


def staged_kit(output, pin):
    """A kit overlay as a mod's bootstrap stages one: its own copy of this checkout, plus the stamp."""
    import importlib.util
    from mod_base.pin import STAMP_NAME, kit_tree_digest, stamp_document
    spec = importlib.util.spec_from_file_location(
        "mod_base_kit_staging_fixture", KIT / "template" / "managed" / "scripts" / "ci" / "mod_base_kit.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    bootstrap.copy_kit(KIT, output)
    digest = kit_tree_digest(output)
    (output / STAMP_NAME).write_bytes(canonical_json(stamp_document(pin, digest)))
    return digest


class LinuxCandidateStagingTests(HostedWorkerCase):
    """The ``stage-candidate`` root operation against a really allocated candidate, with no seam.

    Every case publishes the runner's private request and starts this checkout's bootstrap through
    ``sudo``, exactly as a workflow step will. The original checkout is a real Git checkout made the
    way ``actions/checkout`` makes one for a commit: fetched shallow into a fresh repository and
    detached at the tested commit.
    """

    def staging_fixture(self, *, tracked_out=False):
        from types import SimpleNamespace
        from mod_base.pin import Pin
        base = Path(tempfile.mkdtemp(prefix="mod-base-staging-", dir=os.environ["RUNNER_TEMP"]))
        self.addCleanup(shutil.rmtree, base, True)
        secret = base / "runner-secret"
        secret.write_bytes(b"only the runner and root read this\n")
        secret.chmod(0o600)
        files = {
            ".gitignore": ("100644", b"/build/\n/.gradle/\n/out/reports/\n/out/mod-base-kit/\n"),
            "README.md": ("100644", b"# Staging fixture\n"),
            "binary.bin": ("100644", bytes(range(256)) + b"\r\n\x1a\x00tail"),
            "build.sh": ("100755", b"#!/bin/sh\nset -eu\nmkdir -p build/libs out/reports .gradle\n"
                                   b"cat src/main/resource.txt > build/libs/artifact.txt\n"
                                   b"printf 'report\\n' > out/reports/summary.txt\n"
                                   b"printf 'state\\n' > .gradle/state\n"),
            "docs/latest": ("120000", b"../README.md"),
            "leak": ("120000", os.fsencode(secret)),
            "src/main/empty": ("100644", b""),
            "src/main/resource.txt": ("100644", b"resource\n"),
        }
        if tracked_out:
            files["out/notes.txt"] = ("100644", b"tracked beside the overlay\n")
        upstream = base / "upstream"
        upstream.mkdir()
        staging_git("init", "-q", "--initial-branch=main", ".", cwd=upstream)
        for subject in ("first", "second"):
            (upstream / "README.md").write_bytes(subject.encode() + b"\n")
            staging_git("add", "-A", cwd=upstream)
            staging_git("commit", "-q", "-m", subject, cwd=upstream)
        for name, (mode, data) in files.items():
            leaf = upstream / name
            leaf.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                os.symlink(data, os.fsencode(leaf))
            else:
                leaf.write_bytes(data)
                leaf.chmod(0o755 if mode == "100755" else 0o644)
        staging_git("add", "-A", cwd=upstream)
        staging_git("commit", "-q", "-m", "tested", cwd=upstream)
        commit = staging_git("rev-parse", "HEAD", cwd=upstream).decode().strip()
        source = base / "candidate"
        source.mkdir()
        staging_git("init", "-q", ".", cwd=source)
        staging_git("remote", "add", "origin", f"https://github.com/{STAGING_REPOSITORY}", cwd=source)
        # A token-bearing header and a hook, as a credential-persisting checkout or an earlier step
        # could leave them: neither may reach the candidate.
        staging_git("config", "--local", "http.https://github.com/.extraheader",
                    "AUTHORIZATION: basic fixture-secret", cwd=source)
        hook = source / ".git" / "hooks" / "post-checkout"
        hook.write_bytes(b"#!/bin/sh\necho hook ran >&2\nexit 1\n")
        # A real repository arrives as a pack; this small one would be unpacked without the limit.
        staging_git("-c", "protocol.version=2", "-c", "fetch.unpackLimit=1", "fetch", "-q", "--no-tags", "--depth=2",
                    upstream.as_uri(), "+refs/heads/main:refs/remotes/origin/main", cwd=source)
        staging_git("checkout", "-q", "--force", "--detach", commit, cwd=source)
        hook.chmod(0o755)
        staging_git("tag", "v1", commit, cwd=source)
        self.assertEqual((source / ".git" / "HEAD").read_bytes(), commit.encode() + b"\n")
        self.assertTrue((source / ".git" / "shallow").is_file())
        packs = sorted(os.listdir(source / ".git" / "objects" / "pack"))
        self.assertTrue({name.rsplit(".", 1)[-1] for name in packs} >= {"pack", "idx"}, packs)
        tree = staging_git("rev-parse", "HEAD^{tree}", cwd=source).decode().strip()
        inventory = parse_source_inventory(staging_git("ls-tree", "-r", "-l", "-z", "--full-tree", tree, cwd=source))
        # The inventory Git reports is the authored one: every later comparison is against bytes
        # this test wrote, never against what a copy happens to hold.
        self.assertEqual([(entry.path, entry.mode, entry.size, entry.git_blob) for entry in inventory],
                         [(name, mode, len(data), hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
                          for name, (mode, data) in sorted(files.items())])
        pin = Pin("a" * 40, "v1.0.3", ())
        overlay = base / "kit-overlay"
        digest = staged_kit(overlay, pin)
        seed = base / "gradle-seed"
        cached = {"caches/modules-2/files-2.1/net.fabricmc/yarn/1.20.1+build.10/2d1f/yarn-1.20.1+build.10-v2.jar": b"yarn",
                  "caches/modules-2/modules-2.lock": b"",
                  "wrapper/dists/gradle-8.8-bin/5e/gradle-8.8/lib/gradle-launcher-8.8.jar": b"launcher"}
        for name, data in cached.items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            (seed / name).write_bytes(data)
        return SimpleNamespace(base=base, secret=secret, files=files, source=source, commit=commit, tree=tree,
                               parent=staging_git("rev-parse", "HEAD^", cwd=source).decode().strip(),
                               inventory=inventory, pin=pin, overlay=overlay, digest=digest, seed=seed, cached=cached)

    def stage(self, fixture, **changed):
        """Publish the request and run the root operation, as one lifecycle step does."""
        from mod_base.build_ci.root_request import request_candidate_staging, root_request_path, run_root_operation
        from mod_base.pin import kit_tree_digest
        arguments = dict(repository=STAGING_REPOSITORY, tested_sha=fixture.commit, tested_tree=fixture.tree,
                         inventory=fixture.inventory, source=fixture.source, overlay=fixture.overlay,
                         pin=fixture.pin, expected_digest=fixture.digest, gradle_seed=fixture.seed)
        arguments.update(changed)
        # One request per operation exists at a time; a step that asks again has dropped the old one.
        shutil.rmtree(Path(str(root_request_path("stage-candidate"))), ignore_errors=True)
        nonce = request_candidate_staging(boundary=self.host_boundary,
                                          candidate=authenticate_worker_account("candidate"), **arguments)
        started = time.monotonic()
        try:
            run_root_operation("stage-candidate", python=sys.executable, kit_root=KIT,
                               kit_digest=kit_tree_digest(KIT), nonce=nonce)
        finally:
            print(f"candidate staging root operation: {time.monotonic() - started:.2f}s", file=sys.stderr)

    def as_candidate(self, *args, accepted=(0,)):
        account = authenticate_worker_account("candidate")
        return self.as_worker("candidate", f"HOME={account.home}", "GIT_CONFIG_GLOBAL=/dev/null",
                              "GIT_CONFIG_NOSYSTEM=1", *args, accepted=accepted)

    def as_root(self, *args, accepted=(0,)):
        return command("/usr/bin/sudo", "-n", "--", *args, accepted=accepted)

    def published(self):
        """What exists below the worker root besides the fixture's own directories."""
        return sorted(set(os.listdir(self.root)) - {"candidate-home", "validator-home", "root-request-host-fence",
                                                    "root-request-stage-candidate"})

    def candidate_terminated(self):
        """Whether the candidate was swept and locked: its account then expired on 1970-01-02."""
        shadow = self.as_root("/usr/bin/getent", "shadow", WORKER_ACCOUNTS["candidate"]).stdout.decode()
        return shadow.rstrip("\n").split(":")[7] == "1"

    def test_candidate_reads_builds_and_writes_in_its_checkout_and_never_reaches_the_original(self):
        import json
        from mod_base.pin import stamp_document
        fixture = self.staging_fixture()
        account = authenticate_worker_account("candidate")
        owner = [account.uid, account.gid]
        repository = self.root / "repository"
        secret_before = fixture.secret.stat()
        self.stage(fixture)
        self.assertEqual(self.published(), ["repository"])
        self.assertFalse(self.candidate_terminated())

        # Tracked files: byte for byte the authored tree, with Git's modes, owned by the candidate.
        listed = json.loads(self.as_candidate("/usr/bin/python3", "-I", "-B", "-c", STAGING_LISTING,
                                              str(repository)).stdout)
        expected = {"out": {"kind": "directory", "mode": 0o700, "owner": owner, "links": None}}
        for name, (mode, data) in fixture.files.items():
            parts = name.split("/")
            for count in range(1, len(parts)):
                expected["/".join(parts[:count])] = {"kind": "directory", "mode": 0o700, "owner": owner, "links": None}
            if mode == "120000":
                expected[name] = {"kind": "link", "target": os.fsdecode(data), "mode": None, "owner": owner,
                                  "links": None}
            else:
                expected[name] = {"kind": "file", "hex": data.hex(), "mode": 0o700 if mode == "100755" else 0o600,
                                  "owner": owner, "links": 1}
        inodes = {path: row.pop("inode") for path, row in listed.items()}
        self.assertEqual(listed, expected)
        for name in fixture.files:
            self.assertNotEqual(inodes[name], os.lstat(fixture.source / name).st_ino)

        # The curated .git: the tested commit and tree, a clean work tree, no source config or hook.
        def in_checkout(script):
            return self.as_candidate("/bin/sh", "-euc", f'cd "$1"\n{script}', "sh", str(repository)).stdout.decode()
        self.assertEqual(sorted(in_checkout("ls -A .git").split()),
                         ["HEAD", "config", "index", "objects", "refs", "shallow"])
        self.assertEqual(sorted(in_checkout("ls -A .git/objects/pack").split()),
                         sorted(os.listdir(fixture.source / ".git" / "objects" / "pack")))
        self.assertEqual(in_checkout("git rev-parse HEAD 'HEAD^{tree}'").split(), [fixture.commit, fixture.tree])
        self.assertEqual(in_checkout("git status --porcelain=v1 --untracked-files=all"), "")
        self.assertEqual(in_checkout("git describe --tags"), "v1\n")
        self.assertEqual(in_checkout("git cat-file -p HEAD:src/main/resource.txt"), "resource\n")
        self.assertEqual(in_checkout("git config --get remote.origin.url"),
                         f"https://github.com/{STAGING_REPOSITORY}.git\n")
        self.assertEqual(in_checkout("git config --get core.hooksPath"), "/dev/null\n")
        self.assertEqual(in_checkout("cat .git/HEAD"), fixture.commit + "\n")
        self.assertNotIn("fixture-secret", in_checkout("cat .git/config"))

        # The kit overlay is complete by the kit's own rules, for the pin the runner named.
        stamp = json.loads(self.as_candidate("/usr/bin/python3", "-I", "-B", "-c", STAGING_KIT_PROBE,
                                             str(repository / "out" / "mod-base-kit")).stdout)
        self.assertEqual(stamp, stamp_document(fixture.pin, fixture.digest))
        self.assertEqual(in_checkout("stat -c '%U %a' out/mod-base-kit out/mod-base-kit/src/mod_base/__init__.py"),
                         f"{WORKER_ACCOUNTS['candidate']} 755\n{WORKER_ACCOUNTS['candidate']} 644\n")

        # It builds: the tracked script runs, writes generated roots and beside the overlay, and
        # leaves the work tree clean.
        self.assertEqual(in_checkout("./build.sh && cat build/libs/artifact.txt out/reports/summary.txt .gradle/state"),
                         "resource\nreport\nstate\n")
        self.assertEqual(in_checkout("printf beside > out/beside.txt && cat out/beside.txt"), "beside")
        self.assertEqual(in_checkout("git status --porcelain=v1 --untracked-files=all"), "?? out/beside.txt\n")

        # The seeded cache is the candidate's own private copy.
        home = Path(account.home)
        for name, data in fixture.cached.items():
            self.assertEqual(self.as_candidate("/usr/bin/cat", str(home / "gradle-home" / name)).stdout, data)
        self.as_candidate("/usr/bin/touch", str(home / "gradle-home" / "caches" / "modules-2" / "written.lock"))
        self.assertEqual(self.as_root("/usr/bin/stat", "-c", "%u:%g %a", str(home / "gradle-home"),
                                      str(home / "gradle-home" / "caches" / "modules-2" / "modules-2.lock")).stdout,
                         f"{account.uid}:{account.gid} 700\n{account.uid}:{account.gid} 600\n".encode())

        # The original stays the runner's: the candidate reaches neither it nor what a tracked link
        # names, and staging never touched the link's target.
        for path in (fixture.source / "README.md", fixture.overlay / "MOD_BASE_KIT.json",
                     fixture.seed / "caches" / "modules-2" / "modules-2.lock", fixture.secret):
            self.as_candidate("/usr/bin/cat", str(path), accepted=(1,))
        self.as_candidate("/usr/bin/ls", str(fixture.source), accepted=(2,))
        self.as_candidate("/usr/bin/cat", str(repository / "leak"), accepted=(1,))
        secret_after = fixture.secret.stat()
        self.assertEqual((secret_after.st_ino, secret_after.st_uid, secret_after.st_gid, secret_after.st_mode,
                          secret_after.st_ctime_ns),
                         (secret_before.st_ino, secret_before.st_uid, secret_before.st_gid, secret_before.st_mode,
                          secret_before.st_ctime_ns))
        self.assertEqual(fixture.secret.read_bytes(), b"only the runner and root read this\n")
        # Nobody but the candidate enters the checkout: not the validator, not even the runner.
        self.as_worker("validator", "/usr/bin/ls", str(repository), accepted=(2,))
        with self.assertRaises(PermissionError):
            os.listdir(repository)
        self.assertEqual(staging_git("status", "--porcelain=v1", "--untracked-files=all", cwd=fixture.source), b"")

    def test_second_staging_refuses_the_populated_root_and_locks_the_candidate(self):
        fixture = self.staging_fixture()
        repository = self.root / "repository"
        self.stage(fixture)
        self.as_candidate("/bin/sh", "-euc", 'cd "$1" && ./build.sh', "sh", str(repository))
        before = self.as_root("/usr/bin/find", str(repository), "-printf", "%p %i %T@ %s\\n").stdout
        self.assertIn(b"/repository/out/reports/summary.txt ", before)
        with self.assertRaisesRegex(MbError, "candidate repository already exists; refusing to reuse a populated root"):
            self.stage(fixture)
        with self.assertRaisesRegex(MbError, "candidate repository already exists"):
            self.stage(fixture, gradle_seed=None)
        self.assertEqual(self.as_root("/usr/bin/find", str(repository), "-printf", "%p %i %T@ %s\\n").stdout, before)
        self.assertEqual(self.published(), ["repository"])
        # A refused staging ends the candidate: whatever it left running is swept and it is locked.
        self.assertTrue(self.candidate_terminated())

    def test_unfaithful_checkouts_are_refused_before_anything_is_published(self):
        fixture = self.staging_fixture(tracked_out=True)
        source, account = fixture.source, authenticate_worker_account("candidate")
        moved = fixture.base / "moved"
        other_tree = staging_git("rev-parse", fixture.parent + "^{tree}", cwd=source).decode().strip()

        def link_file():
            os.rename(source / "src" / "main" / "resource.txt", moved)
            os.symlink(moved, source / "src" / "main" / "resource.txt")

        def unlink_file():
            os.unlink(source / "src" / "main" / "resource.txt")
            os.rename(moved, source / "src" / "main" / "resource.txt")

        def link_directory():
            os.rename(source / "src", moved)
            os.symlink(moved, source / "src")

        def unlink_directory():
            os.unlink(source / "src")
            os.rename(moved, source / "src")

        def on_a_branch():
            staging_git("update-ref", "refs/heads/work", fixture.commit, cwd=source)
            staging_git("symbolic-ref", "HEAD", "refs/heads/work", cwd=source)

        def off_the_branch():
            staging_git("update-ref", "--no-deref", "HEAD", fixture.commit, cwd=source)
            staging_git("update-ref", "-d", "refs/heads/work", cwd=source)

        cases = (
            ("tracked file replaced by a symlink", link_file, unlink_file, {},
             "tracked source differs from the protected tested tree"),
            ("tracked directory replaced by a symlink", link_directory, unlink_directory, {},
             "path has an unsafe parent component"),
            ("planted symlink", lambda: os.symlink("/etc/hostname", source / "planted"),
             lambda: os.unlink(source / "planted"), {}, "source contains an undeclared path"),
            ("hard link", lambda: os.link(source / "README.md", moved), lambda: os.unlink(moved), {},
             "tracked source has multiple hard links"),
            ("undeclared file", lambda: (source / "src" / "main" / "untracked.txt").write_bytes(b"x"),
             lambda: os.unlink(source / "src" / "main" / "untracked.txt"), {}, "source contains an undeclared path"),
            ("changed byte", lambda: (source / "README.md").write_bytes(b"# Staging fixturE\n"),
             lambda: (source / "README.md").write_bytes(b"# Staging fixture\n"), {},
             "tracked source differs from the protected tested tree"),
            ("HEAD moved to another commit",
             lambda: staging_git("update-ref", "--no-deref", "HEAD", fixture.parent, cwd=source),
             lambda: staging_git("update-ref", "--no-deref", "HEAD", fixture.commit, cwd=source), {},
             "Git source HEAD is not detached at the tested commit"),
            ("HEAD moved to a branch", on_a_branch, off_the_branch, {},
             "Git source HEAD is not detached at the tested commit"),
            ("inventory of another tree", lambda: None, lambda: None, {"tested_tree": other_tree},
             "source inventory is not the inventory of the tested tree"),
            ("another commit named", lambda: None, lambda: None, {"tested_sha": fixture.parent},
             "Git source HEAD is not detached at the tested commit"),
            ("overlay of another digest", lambda: None, lambda: None, {"expected_digest": "sha256:" + "0" * 64},
             "worker kit overlay differs from the independently bound pin/digest"),
            ("overlapping roots", lambda: None, lambda: None, {"gradle_seed": fixture.overlay / "src"},
             "source/cache/overlay roots overlap"),
        )
        for name, mutate, restore, changed, message in cases:
            with self.subTest(name=name):
                mutate()
                try:
                    with self.assertRaisesRegex(MbError, message):
                        self.stage(fixture, **changed)
                finally:
                    restore()
                self.assertEqual(self.published(), [])
                self.assertEqual(sorted(self.as_root("/usr/bin/find", account.home, "-mindepth", "1", "-printf",
                                                     "%P %U %m\\n").stdout.decode().splitlines()),
                                 [f"gradle-home {account.uid} 700", f"tmp {account.uid} 700"])
                self.assertTrue(self.candidate_terminated())

        # The same checkout, restored, stages: every refusal above was for its own defect. This
        # tree tracks a file below out/, so the directory comes with the source, and no seed is
        # given, so the allocated cache stays empty.
        self.stage(fixture, gradle_seed=None)
        repository = self.root / "repository"
        self.assertEqual(self.as_root("/usr/bin/stat", "-c", "%u:%g %a %n", str(repository), str(repository / "out"),
                                      str(repository / "out" / "notes.txt"), str(repository / "out" / "mod-base-kit"),
                                      str(Path(account.home) / "gradle-home")).stdout.decode().splitlines(),
                         [f"{account.uid}:{account.gid} 700 {repository}", f"{account.uid}:{account.gid} 700 {repository}/out",
                          f"{account.uid}:{account.gid} 600 {repository}/out/notes.txt",
                          f"{account.uid}:{account.gid} 755 {repository}/out/mod-base-kit",
                          f"{account.uid}:{account.gid} 700 {account.home}/gradle-home"])
        self.assertEqual(self.as_root("/usr/bin/cat", str(repository / "out" / "notes.txt")).stdout,
                         b"tracked beside the overlay\n")
        self.assertEqual(self.as_root("/usr/bin/find", str(Path(account.home) / "gradle-home"), "-mindepth", "1").stdout,
                         b"")

    def test_request_refuses_what_root_could_never_admit_and_publishes_one_private_record(self):
        import dataclasses
        import json
        from mod_base.build_ci.root_request import request_candidate_staging, root_request_path
        from mod_base.pin import Pin
        fixture = self.staging_fixture()
        candidate = authenticate_worker_account("candidate")
        valid = dict(boundary=self.host_boundary, candidate=candidate, repository=STAGING_REPOSITORY,
                     tested_sha=fixture.commit, tested_tree=fixture.tree, inventory=fixture.inventory,
                     source=fixture.source, overlay=fixture.overlay, pin=fixture.pin,
                     expected_digest=fixture.digest, gradle_seed=fixture.seed)
        request = Path(str(root_request_path("stage-candidate")))
        cases = {
            "the validator as candidate": {"candidate": authenticate_worker_account("validator")},
            "an account that was never allocated": {"candidate": dataclasses.replace(candidate, uid=candidate.uid + 7)},
            "another runner's fence": {"boundary": dataclasses.replace(self.host_boundary,
                                                                       inode=self.host_boundary.inode + 1)},
            "a source outside the fenced home": {"source": self.root / "elsewhere"},
            "a source given as text": {"source": str(fixture.source)},
            "an overlay outside the fenced home": {"overlay": Path("/opt/kit-overlay")},
            "a seed outside the fenced home": {"gradle_seed": Path("/var/cache/gradle")},
            "an unsorted inventory": {"inventory": tuple(reversed(fixture.inventory))},
            "an inventory given as a list": {"inventory": list(fixture.inventory)},
            "an empty inventory": {"inventory": ()},
            "a pin without its tag form": {"pin": Pin("a" * 40, "1.0.3", ())},
            "a digest without its algorithm": {"expected_digest": "0" * 64},
            "a branch name for the commit": {"tested_sha": "main"},
            "a repository with a path": {"repository": STAGING_REPOSITORY + "/tree/main"},
        }
        for name, changed in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(MbError):
                    request_candidate_staging(**{**valid, **changed})
                self.assertFalse(request.exists())
        nonce = request_candidate_staging(**valid)
        self.assertRegex(nonce, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(os.listdir(request), [grammar.CI_ROOT_REQUEST_NAME])
        record = request / grammar.CI_ROOT_REQUEST_NAME
        for path, mode in ((request, 0o700), (record, 0o600)):
            info = path.lstat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (os.getuid(), os.getgid(), mode))
        raw = record.read_bytes()
        document = json.loads(raw)
        self.assertEqual(raw, canonical_json(document))
        self.assertEqual((document["operation"], document["nonce"]), ("stage-candidate", nonce))
        self.assertEqual(document["arguments"],
                         {"candidate": {"uid": candidate.uid, "gid": candidate.gid}, "repository": STAGING_REPOSITORY,
                          "tested_sha": fixture.commit, "tested_tree": fixture.tree,
                          "inventory": [{"path": entry.path, "mode": entry.mode, "size": entry.size,
                                         "git_blob": entry.git_blob} for entry in fixture.inventory],
                          "source": str(fixture.source), "gradle_seed": str(fixture.seed),
                          "overlay": {"path": str(fixture.overlay), "sha": fixture.pin.sha, "version": "1.0.3",
                                      "tree_digest": fixture.digest}})
        # A request is data: nothing is staged until the bootstrap runs, and one never replaces another.
        self.assertEqual(self.published(), [])
        with self.assertRaises(MbError):
            request_candidate_staging(**valid)
        self.assertEqual(record.read_bytes(), raw)

    def test_candidate_that_already_wrote_its_cache_is_refused_with_nothing_published(self):
        fixture = self.staging_fixture()
        account = authenticate_worker_account("candidate")
        self.as_candidate("/usr/bin/touch", str(Path(account.home) / "gradle-home" / "left-by-an-earlier-run"))
        for seed in (fixture.seed, None):
            with self.subTest(seeded=seed is not None):
                with self.assertRaisesRegex(MbError, "allocated Gradle cache is not empty; refusing reuse"):
                    self.stage(fixture, gradle_seed=seed)
                self.assertEqual(self.published(), [])
        self.assertTrue(self.candidate_terminated())

    def test_seed_with_configuration_is_refused_and_the_candidate_never_runs(self):
        fixture = self.staging_fixture()
        account = authenticate_worker_account("candidate")
        (fixture.seed / "gradle.properties").write_bytes(b"orgSecret=fixture credential\n")
        with self.assertRaisesRegex(MbError, "Gradle seed includes a configuration/credential or unexpected root"):
            self.stage(fixture)
        # The seed is admitted when its phase starts, after source and Git data were published:
        # nothing of it was copied, the overlay phase never ran and the candidate is locked.
        self.assertEqual(self.as_root("/usr/bin/find", str(Path(account.home) / "gradle-home"), "-mindepth", "1").stdout,
                         b"")
        self.assertTrue(self.candidate_terminated())
        self.as_root("/usr/bin/test", "!", "-e", str(self.root / "repository" / "out"))


class LinuxLifecycleCommandTests(unittest.TestCase):
    """The job steps as a workflow runs them: real commands, accounts, sudo and root operations.

    ``ci subject`` -> ``ci worker-prepare`` -> ``ci plan`` -> ``ci worker-finish`` on the synthetic
    mod. Only the GitHub API is a fake. Every case starts without a boundary and without accounts
    and removes both afterwards, so the cases do not depend on each other.
    """

    HOME = Path("/home/runner")
    PLAN_HOOK = "[validator] synthetic derive_plan: ok, 1 files\n"

    def setUp(self):
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" \
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("UID integration tests require a fresh GitHub-hosted Linux runner")
        import pwd

        command("/usr/bin/sudo", "-n", "/usr/bin/true")
        self.root = Path(str(WORKER_ROOT))
        if self.root.parent.exists() or self.root.parent.is_symlink():
            raise AssertionError("Linux fixture boundary already exists; refusing to repurpose it")
        for name in WORKER_ACCOUNTS.values():
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            raise AssertionError("Linux fixture account already exists; refusing to repurpose it")
        self.home_mode = stat.S_IMODE(self.HOME.stat().st_mode)
        self.assertNotEqual(self.home_mode, 0o700, "the fixture needs a home whose closing can be observed")
        self.addCleanup(self.cleanup)
        directory = tempfile.TemporaryDirectory(prefix="mod-base-lifecycle-", dir=os.environ["RUNNER_TEMP"])
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name)
        self.state = self.temporary / "state"
        self.output = self.temporary / "github-output"
        self.api, self.pull = h.github()
        self.environment = {**h.environment(), "RUNNER_ENVIRONMENT": "github-hosted",
                            "GITHUB_WORKSPACE": os.environ["GITHUB_WORKSPACE"],
                            "RUNNER_TEMP": os.environ["RUNNER_TEMP"]}

    def cleanup(self):
        import pwd

        for name in WORKER_ACCOUNTS.values():
            try:
                uid = pwd.getpwnam(name).pw_uid
            except KeyError:
                continue
            for flag in ("-u", "-U"):
                command("/usr/bin/sudo", "-n", "/usr/bin/pkill", "-KILL", flag, str(uid), accepted=(0, 1))
            command("/usr/bin/sudo", "-n", "/usr/sbin/userdel", name)
        command("/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", "/tmp/mod-base-sandbox-boundary")
        os.chmod(self.HOME, self.home_mode)

    # -- helpers -------------------------------------------------------------------------------------

    def job(self, *, faults=None, candidate=False, mod=None):
        """The commands of one job on a copy of the synthetic mod; ``ci subject`` has run.

        Without ``candidate`` the tested tree is served by the fake API. With it, the job has a
        candidate checkout whose commit is the pull request's test merge.
        """
        self.mod = mod or h.materialize(self.temporary / "mod", faults=faults)
        self.checkout = None
        if candidate:
            self.checkout = self.temporary / "candidate"
            commit, tree = job_fixture.commit_candidate(self.mod, self.checkout)
            job_fixture.retarget(self.api, self.pull, commit, tree)
        else:
            job_fixture.seed_tested_tree(self.api, self.mod)
        self.commands = job_fixture.Commands(self.mod, self.state, self.api, self.environment)
        self.commands.subject(self.output)
        self.subject = identity.read_subject(self.state)["subject"]
        return self.commands

    def prepare(self, roles, *extra):
        return self.commands.run("worker-prepare", "--roles", roles, "--python", sys.executable, *extra)

    def plan(self, *extra):
        source = () if self.checkout is None else ("--candidate", str(self.checkout))
        return self.commands.run("plan", *source, "--github-output", str(self.output), *extra)

    def finish(self):
        return self.commands.run("worker-finish")

    def expected_plan(self):
        """The plan the pure planning functions give for the same subject, config and candidate
        files, with ``derive_plan`` run as a plain process."""
        index = len(list(self.temporary.glob("pure-*")))
        sandbox = h.Sandbox(self.temporary / f"pure-{index}", protected=self.mod)
        sandbox.subject = self.subject
        return sandbox.derive_plan()

    def account(self, role):
        import pwd

        try:
            return pwd.getpwnam(WORKER_ACCOUNTS[role])
        except KeyError:
            return None

    def as_account(self, role, *args):
        """The exit status of a command run as the account (which may be locked)."""
        record = self.account(role)
        return command("/usr/bin/sudo", "-n", "--", "/usr/bin/setpriv", "--no-new-privs", f"--reuid={record.pw_uid}",
                       f"--regid={record.pw_gid}", "--clear-groups", "--", "/usr/bin/env", "-i", "--chdir=/",
                       "PATH=/usr/bin:/bin", *args, accepted=(0, 1), cwd="/").returncode

    def assert_resting(self, *roles):
        """Exactly ``roles`` have an account; each is locked, expired and owns no process."""
        for role in WORKER_ACCOUNTS:
            record = self.account(role)
            if role not in roles:
                self.assertIsNone(record, role)
                continue
            self.assertIsNotNone(record, role)
            shadow = command("/usr/bin/sudo", "-n", "/usr/bin/getent", "shadow", record.pw_name).stdout.split(b":")
            self.assertTrue(shadow[1].startswith(b"!"), role)
            self.assertEqual(shadow[7], b"1", role)
            for flag in ("-u", "-U"):
                self.assertEqual(command("/usr/bin/sudo", "-n", "/usr/bin/pgrep", flag, str(record.pw_uid),
                                         accepted=(0, 1)).stdout, b"", role)

    def home(self):
        return stat.S_IMODE(self.HOME.stat().st_mode)

    def jdk(self, name):
        """A stand-in JDK home below /opt: root's, closed, like a tool tree after the fence."""
        home = Path("/opt") / f"mod-base-lifecycle-jdk-{os.getpid()}-{name}"
        command("/usr/bin/sudo", "-n", "/usr/bin/mkdir", "--", str(home), str(home / "bin"))
        command("/usr/bin/sudo", "-n", "/usr/bin/touch", "--", str(home / "release"))
        self.addCleanup(command, "/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(home))
        return str(home)

    def owner(self, path):
        info = os.lstat(path)
        return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)

    def assert_finished(self, *roles):
        """``worker-finish`` succeeds, twice, and leaves the home as it was before the job."""
        states = ", ".join(f"{role} {'locked' if role in roles else 'absent'}" for role in WORKER_ACCOUNTS)
        for _ in range(2):
            self.assertEqual(self.finish(), (0, f"worker-finish: {states}; no process left; runner home mode "
                                                f"{self.home_mode:04o} restored\n", ""))
            self.assertEqual(self.home(), self.home_mode)
            self.assert_resting(*roles)

    # -- the sequence ---------------------------------------------------------------------------------

    def test_validator_job_plans_from_the_api_and_finishes(self):
        commands = self.job()
        code, stdout, stderr = self.prepare("validator")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertRegex(stdout, r"^worker-prepare: validator allocated and locked; \d+ tool entries admitted "
                                 r"under \d+ roots\n$")
        self.assertEqual(self.home(), 0o700)
        self.assert_resting("validator")
        validator = self.account("validator")
        worker = lifecycle.read_worker(self.state)
        self.assertEqual(set(worker.accounts), {"validator"})
        self.assertEqual((worker.validator.uid, worker.validator.gid), (validator.pw_uid, validator.pw_gid))
        self.assertEqual((worker.python, worker.java_homes, worker.boundary.original_mode),
                         (sys.executable, (), self.home_mode))
        self.assertEqual({path.name for path in self.state.iterdir()},
                         {"identity.json", "worker-host.json", "worker.json"})
        # The validator reads the protected adapter copy and nothing of the runner's.
        controller = self.root / "controller"
        dispatcher = controller / "scripts/ci/mod_base_build_dispatch.py"
        self.assertEqual(self.owner(controller), (os.getuid(), validator.pw_gid, 0o750))
        self.assertEqual(self.owner(dispatcher), (os.getuid(), validator.pw_gid, 0o640))
        self.assertEqual(dispatcher.read_bytes(), (self.mod / "scripts/ci/mod_base_build_dispatch.py").read_bytes())
        self.assertEqual({path.relative_to(controller).as_posix() for path in controller.rglob("*") if path.is_file()},
                         {"scripts/ci/mod-base-build.json", "scripts/ci/mod_base_build_adapter.py",
                          "scripts/ci/mod_base_build_dispatch.py", "scripts/ci/policy_suite.py"})
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(dispatcher)), 0)
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-w", str(dispatcher)), 1)
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-w", str(controller)), 1)
        for private in (self.state, self.mod, self.HOME, self.root / "root-request-grant-controller"):
            self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(private)), 1, private)
        self.assertEqual(self.api.request_count, 4)  # Preparing a worker reads nothing from the API.

        expected = self.expected_plan()
        sources = {"inventory": (self.mod / "release/inventory.json").read_bytes(),
                   "scenario-contract": (self.mod / "e2e/scenario-contract.json").read_bytes(),
                   "gradle-properties": (self.mod / "gradle.properties").read_bytes()}
        self.assertEqual([item["name"] for item in expected["plan_inputs"]], ["gradle-properties"])
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(stdout, self.PLAN_HOOK + f"plan: {expected['plan_sha256']} with 2 targets and 3 lanes\n")
        self.assertEqual((self.state / "ci-plan.json").read_bytes(), canonical_json(expected))
        self.assertEqual(self.owner(self.state / "ci-plan.json")[2], 0o600)
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         f"tested_sha={h.TESTED_SHA}\npr_number=7\nplan_sha256={expected['plan_sha256']}\n"
                         'targets=["1.20.1","1.21.1"]\nlanes=["fabric-1.20.1","forge-1.20.1","fabric-1.21.1"]\n')
        # Rule 4: the tested tree and one blob per candidate file, through a client with the pinned budget.
        self.assertEqual(self.api.request_count, 4 + 1 + len(sources))
        self.assertEqual(commands.budgets, [limits.MAX_CI_SUBJECT_REQUESTS, limits.MAX_CI_PLAN_REQUESTS])
        self.assertEqual(self.api.mutations, [])
        self.assert_resting("validator")
        # Every later protected hook finds its complete input: the plan and the files it binds.
        inputs = self.root / "validation-input"
        self.assertEqual(self.owner(inputs), (os.getuid(), validator.pw_gid, 0o750))
        self.assertEqual({path.name: path.read_bytes() for path in inputs.iterdir()},
                         {"ci-plan.json": canonical_json(expected), **sources})
        for leaf in inputs.iterdir():
            self.assertEqual(self.owner(leaf), (os.getuid(), validator.pw_gid, 0o640))
            self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(leaf)), 0)
            self.assertEqual(self.as_account("validator", "/usr/bin/test", "-w", str(leaf)), 1)
        # What the hook wrote was handed to the runner as a private copy and removed from its home.
        derived = self.root / "derived-plan"
        self.assertEqual(self.owner(derived), (os.getuid(), os.getgid(), 0o700))
        self.assertEqual(self.owner(derived / "plan.json"), (os.getuid(), os.getgid(), 0o600))
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(derived / "plan.json")), 1)
        home = self.root / "validator-home"
        self.assertEqual(sorted(command("/usr/bin/sudo", "-n", "/usr/bin/find", str(home), "-mindepth", "1",
                                        "-maxdepth", "1", "-printf", "%f\\n").stdout.split()),
                         [b"gradle-home", b"tmp"])

        # A plan is derived once; the second attempt changes nothing.
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("ci-lifecycle: this job already has a plan", stderr)
        self.assertEqual((self.state / "ci-plan.json").read_bytes(), canonical_json(expected))

        self.assert_finished("validator")
        # After the sweep the home is open again, so no hook can be started for this job any more.
        (self.state / "ci-plan.json").unlink()
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("ci-worker: runner host fence identity or private mode changed", stderr)
        self.assert_resting("validator")

    def test_candidate_and_validator_job_plans_from_the_candidate_checkout_without_the_api(self):
        commands = self.job(candidate=True)
        # The working files are not what is tested: only the commit's objects are read.
        (self.checkout / "release/inventory.json").write_bytes(b"{}\n")
        (self.checkout / "e2e/scenario-contract.json").unlink()
        code, stdout, stderr = self.prepare("candidate+validator")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertIn("candidate and validator allocated and locked", stdout)
        self.assert_resting("candidate", "validator")
        candidate, validator = self.account("candidate"), self.account("validator")
        self.assertNotEqual((candidate.pw_uid, candidate.pw_gid), (validator.pw_uid, validator.pw_gid))
        self.assertEqual(self.as_account("candidate", "/usr/bin/test", "-r", str(self.root / "controller")), 1)
        expected = self.expected_plan()
        self.assertEqual(expected["identity"]["tested_sha"], job_fixture.git(self.checkout, "rev-parse", "HEAD"))

        wrong = "0" * 64
        code, stdout, stderr = self.plan("--expect-sha256", wrong)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, self.PLAN_HOOK)
        self.assertEqual(stderr, "mod_base: plan-mismatch: this job derived another plan than the one the "
                                 "generation agreed on\n")
        self.assertFalse((self.state / "ci-plan.json").exists())
        self.assertEqual(self.output.read_text(encoding="utf-8").count("\n"), 2)  # Only `ci subject` wrote.
        self.assert_resting("candidate", "validator")
        self.assertEqual(self.as_account("candidate", "/usr/bin/test", "-r", str(self.root / "validation-input")), 1)
        self.assertEqual((self.api.request_count, commands.budgets), (4, [limits.MAX_CI_SUBJECT_REQUESTS]))
        self.assert_finished("candidate", "validator")

    def test_the_agreed_plan_hash_is_accepted_and_a_locked_validator_runs_the_next_protected_hook(self):
        self.job(candidate=True)
        self.assertEqual(self.prepare("candidate+validator")[0], 0)
        expected = self.expected_plan()
        code, stdout, stderr = self.plan("--expect-sha256", expected["plan_sha256"])
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual((self.state / "ci-plan.json").read_bytes(), canonical_json(expected))
        self.assert_resting("candidate", "validator")
        # What the next commands build on: the locked validator runs a hook against the plan, from
        # the same adapter copy and input root, and finds no output of the hook before it.
        from mod_base import runtime
        job = lifecycle.open_job(runtime.build_invocation(self.mod, None, self.environment), self.state)
        worker = lifecycle.open_worker(job)
        plan = lifecycle.read_plan(job)
        self.assertEqual(plan, expected)
        log = []
        result = lifecycle.run_protected_hook(job, worker, "derive_runtime", plan=plan, unit_id="forge-1.20.1",
                                              log=log.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(log, ["[validator] synthetic derive_runtime forge-1.20.1: ok, 1 files\n"])
        self.assertEqual(command("/usr/bin/sudo", "-n", "/usr/bin/find", str(self.root / "validator-home/validation"),
                                 "-type", "f", "-printf", "%f %m\\n").stdout, b"runtime.json 600\n")
        with self.assertRaises(MbError):  # A lane the plan does not hold never reaches the account.
            lifecycle.run_protected_hook(job, worker, "derive_runtime", plan=plan, unit_id="quilt-1.20.1",
                                         log=log.append)
        self.assertEqual(len(log), 1)
        self.assert_resting("candidate", "validator")
        self.assert_finished("candidate", "validator")

    def test_a_hook_that_never_sets_its_umask_still_leaves_private_output(self):
        mod = h.materialize(self.temporary / "mod")
        dispatcher = mod / "scripts/ci/mod_base_build_dispatch.py"
        source = dispatcher.read_text(encoding="utf-8")
        self.assertEqual(source.count("    os.umask(0o077)\n"), 1)
        dispatcher.write_text(source.replace(
            "    os.umask(0o077)\n",
            "    inherited = os.umask(0)\n    os.umask(inherited)\n"
            "    print(f'synthetic inherited umask {inherited:03o}', flush=True)\n"), encoding="utf-8", newline="\n")
        job_fixture.rewrite_config(mod)
        self.job(mod=mod)
        self.assertEqual(self.prepare("validator")[0], 0)
        expected = self.expected_plan()
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(stdout, "[validator] synthetic inherited umask 077\n" + self.PLAN_HOOK
                         + f"plan: {expected['plan_sha256']} with 2 targets and 3 lanes\n")
        self.assert_finished("validator")

    def test_jdk_homes_are_admitted_recorded_in_order_and_the_first_is_a_hooks_java_home(self):
        first, second = self.jdk("17"), self.jdk("21")
        mod = h.materialize(self.temporary / "mod")
        dispatcher = mod / "scripts/ci/mod_base_build_dispatch.py"
        source = dispatcher.read_text(encoding="utf-8")
        self.assertEqual(source.count("    os.umask(0o077)\n"), 1)
        dispatcher.write_text(source.replace(
            "    os.umask(0o077)\n",
            "    os.umask(0o077)\n"
            "    print('synthetic JAVA_HOME', os.environ.get('JAVA_HOME'), os.environ['PATH'].split(':')[1],"
            " flush=True)\n"), encoding="utf-8", newline="\n")
        job_fixture.rewrite_config(mod)
        self.job(mod=mod)
        code, stdout, stderr = self.prepare("validator", "--java-home", first, "--java-home", second)
        self.assertEqual((code, stderr), (0, ""), stdout)
        worker = lifecycle.read_worker(self.state)
        self.assertEqual((worker.java_homes, worker.java_home), ((first, second), first))
        self.assertEqual(worker.tools.roots[-2:], (first, second))
        self.assertEqual(len(set(worker.tools.roots)), len(worker.tools.roots))
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertTrue(stdout.startswith(f"[validator] synthetic JAVA_HOME {first} {first}/bin\n"), stdout)
        self.assert_finished("validator")

    def test_a_changed_config_or_tool_tree_after_prepare_stops_the_next_command(self):
        home = self.jdk("drift")
        self.job()
        self.assertEqual(self.prepare("validator", "--java-home", home)[0], 0)
        config = self.mod / "scripts/ci/mod-base-build.json"
        original = config.read_bytes()
        job_fixture.rewrite_config(self.mod, validator_seconds=7)
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("the protected Build config changed after the worker was prepared", stderr)
        config.write_bytes(original)
        # The interpreter and the JDKs are what the next root operation and the next hook run from.
        command("/usr/bin/sudo", "-n", "/usr/bin/touch", "--", str(Path(home) / "bin/java"))
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "mod_base: ci-worker: tool closure changed after protected admission\n")
        self.assertFalse((self.state / "ci-plan.json").exists())
        self.assertFalse((self.root / "validation-input").exists())
        self.assertEqual(self.api.request_count, 4)  # Nothing was read for a worker that cannot be opened.
        self.assert_resting("validator")
        self.assert_finished("validator")

    # -- faults ---------------------------------------------------------------------------------------

    def fault(self, mode, *, error, log=None, timeout=None):
        """A ``derive_plan`` that misbehaves is a clean rejection: no plan, both accounts locked,
        nothing of theirs running, and the sweep still restores the home."""
        mod = h.materialize(self.temporary / "mod", faults=[{"hook": "derive_plan", "unit": None, "mode": mode}])
        if timeout is not None:
            job_fixture.rewrite_config(mod, validator_seconds=timeout)
        self.job(mod=mod, candidate=True)
        self.assertEqual(self.prepare("candidate+validator")[0], 0)
        started = time.monotonic()
        code, stdout, stderr = self.plan()
        self.assertEqual(code, 2, stdout + stderr)
        self.assertRegex(stderr, error)
        self.assertEqual(stderr.count("\n"), 1)
        if log is not None:
            self.assertEqual(stdout, log)
        self.assertLess(time.monotonic() - started, 60)
        self.assertFalse((self.state / "ci-plan.json").exists())
        self.assertEqual(self.output.read_text(encoding="utf-8").count("\n"), 2)
        self.assertFalse((self.root / "derived-plan").exists())
        self.assertEqual(self.home(), 0o700)
        self.assert_resting("candidate", "validator")
        self.assert_finished("candidate", "validator")

    def test_failing_hook_is_rejected_with_its_log(self):
        self.fault("fail", error=r"^mod_base: ci-worker: worker dispatcher returned failure\n$",
                   log="[validator] synthetic derive_plan rejected: the release inventory requests this failure\n")

    def test_hanging_hook_is_killed_at_the_protected_timeout(self):
        self.fault("hang", timeout=3, error=r"^mod_base: ci-worker: worker execution timed out\n$", log="")

    def test_hook_that_leaves_an_orphan_process_is_rejected_and_the_orphan_is_killed(self):
        self.fault("orphan", error=r"^mod_base: ci-worker: worker dispatcher left a process behind\n$")

    def test_hook_that_writes_an_extra_file_is_rejected_by_root(self):
        self.fault("extra", error=r"^mod_base: ci-worker: root operation take-derived-plan failed with exit 2: ")
        self.assertTrue(command("/usr/bin/sudo", "-n", "/usr/bin/test", "-e",
                                str(self.root / "validator-home/validation/unplanned.txt")).returncode == 0)

    def test_hook_that_omits_its_output_is_rejected_by_root(self):
        self.fault("missing", error=r"^mod_base: ci-worker: root operation take-derived-plan failed with exit 2: ")

    # -- prepare and finish in every state --------------------------------------------------------------

    def test_second_worker_prepare_of_a_job_refuses_and_changes_nothing(self):
        self.job()
        self.assertEqual(self.prepare("candidate+validator")[0], 0)
        accounts = {role: self.account(role).pw_uid for role in WORKER_ACCOUNTS}
        record = (self.state / "worker.json").read_bytes()
        for roles in ("candidate+validator", "validator"):
            code, stdout, stderr = self.prepare(roles)
            self.assertEqual((code, stdout), (2, ""))
            self.assertEqual(stderr, "mod_base: ci-state: cannot write the state record worker-host.json: "
                                     "File exists\n")
        self.assertEqual({role: self.account(role).pw_uid for role in WORKER_ACCOUNTS}, accounts)
        self.assertEqual((self.state / "worker.json").read_bytes(), record)
        self.assertEqual(self.home(), 0o700)
        self.assert_resting("candidate", "validator")
        self.assert_finished("candidate", "validator")

    def test_finish_after_a_prepare_that_failed_before_any_account_existed(self):
        self.job()
        code, stdout, stderr = self.prepare("candidate+validator", "--java-home", "/opt/mod-base-no-such-jdk")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("ci-worker: cannot inspect protected host tool closure", stderr)
        self.assertEqual(self.home(), 0o700)  # A failed prepare never opens the home again by itself.
        self.assertFalse((self.state / "worker.json").exists())
        self.assert_resting()
        self.assertEqual(self.plan()[0], 2)  # No worker: nothing can be planned.
        self.assert_finished()

    def test_finish_after_a_prepare_that_failed_once_the_accounts_existed(self):
        self.job()
        with patch.object(lifecycle, "request_controller_grant", side_effect=MbError("injected failure")):
            code, stdout, stderr = self.prepare("candidate+validator")
        self.assertEqual((code, stdout, stderr), (2, "", "mod_base: rejected: injected failure\n"))
        self.assertEqual(self.home(), 0o700)
        self.assertFalse((self.state / "worker.json").exists())
        self.assert_resting("candidate", "validator")  # The failed prepare locked what it had allocated.
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(self.root / "controller")), 1)
        self.assertEqual(self.plan()[0], 2)
        self.assert_finished("candidate", "validator")

    def test_finish_of_a_job_that_never_prepared_touches_nothing(self):
        self.job()
        untouched = (0, "worker-finish: candidate absent, validator absent; no process left; runner home "
                        "untouched by this job\n", "")
        self.assertEqual(self.finish(), untouched)
        self.assertEqual(self.commands.run("worker-finish", state=self.temporary / "no-such-state"), untouched)
        self.assertEqual(self.home(), self.home_mode)
        self.assertFalse(self.root.parent.exists())
        self.assert_resting()

    def test_finish_stops_a_process_that_outlived_its_step_and_then_restores_the_home(self):
        self.job()
        self.assertEqual(self.prepare("candidate+validator")[0], 0)
        # What a cancelled step leaves: processes of both accounts that nobody waits for any more.
        for role in WORKER_ACCOUNTS:
            record = self.account(role)
            stray = subprocess.Popen(("/usr/bin/sudo", "-n", "--user", f"#{record.pw_uid}", "--", "/usr/bin/setsid",
                                      "--fork", "/usr/bin/sleep", "600"), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=HOST_ENV, cwd="/")
            self.assertEqual(stray.wait(timeout=20), 0)
            self.assertNotEqual(command("/usr/bin/sudo", "-n", "/usr/bin/pgrep", "-u", str(record.pw_uid)).stdout, b"")
        self.assert_finished("candidate", "validator")

    def test_finish_keeps_the_home_closed_when_an_account_is_not_the_one_this_kit_allocates(self):
        self.job()
        self.assertEqual(self.prepare("validator")[0], 0)
        # A candidate account with another home: its UID is never signalled, and the home stays closed.
        command("/usr/bin/sudo", "-n", "/usr/sbin/useradd", "--no-create-home", "--home-dir", "/nonexistent",
                "--shell", "/bin/bash", "--user-group", WORKER_ACCOUNTS["candidate"])
        code, stdout, stderr = self.finish()
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("$.account.home: worker passwd home changed", stderr)
        self.assertEqual(self.home(), 0o700)
        self.assert_resting("candidate", "validator")  # Both are locked all the same.


class LinuxWorkerValidateTests(unittest.TestCase):
    """``ci worker-validate`` as the seal step of a job: real commands, accounts, sudo and root.

    ``ci subject`` -> ``ci worker-prepare`` -> ``ci plan`` -> a sealed export in place ->
    ``ci worker-validate`` -> ``ci worker-finish`` on the synthetic mod. The sealed export is laid
    down as the step before leaves it: the complete Build by ``exports.assemble_build_export``
    (``ci assemble``), a target's partition and a lane's results as the runner-private copy with
    its envelope that ``ci worker-seal`` freezes. Their bytes come from the synthetic mod's
    candidate hooks, run as plain processes for the job's own plan. Only the GitHub API is a
    fake. Every case starts without a boundary and without accounts and removes both afterwards.
    """

    HOME = LinuxLifecycleCommandTests.HOME
    RUN = {"build": "42", "packaged": "43"}
    cleanup = LinuxLifecycleCommandTests.cleanup
    prepare = LinuxLifecycleCommandTests.prepare
    plan = LinuxLifecycleCommandTests.plan
    finish = LinuxLifecycleCommandTests.finish
    account = LinuxLifecycleCommandTests.account
    as_account = LinuxLifecycleCommandTests.as_account
    assert_resting = LinuxLifecycleCommandTests.assert_resting
    assert_finished = LinuxLifecycleCommandTests.assert_finished
    home = LinuxLifecycleCommandTests.home
    owner = LinuxLifecycleCommandTests.owner

    def setUp(self):
        LinuxLifecycleCommandTests.setUp(self)
        self.upload = self.temporary / "mb-upload"
        self.build = Path(str(BUILD_VALIDATION_ROOT))
        self.sealed = self.root / "sealed-validation"

    # -- the job and its sealed export ------------------------------------------------------------------

    def begin(self, roles, *, producer="build", faults=None, timeout=None):
        """One job up to its plan: ``ci subject``, ``ci worker-prepare --roles`` and ``ci plan``.

        A job with a candidate has its checkout; the other reads the tested tree from the API.
        Returns the plan the job recorded.
        """
        from mod_base import runtime

        self.mod = h.materialize(self.temporary / "mod", faults=faults)
        if timeout is not None:
            job_fixture.rewrite_config(self.mod, validator_seconds=timeout)
        self.environment = {**self.environment, **h.environment(caller=producer),
                            "GITHUB_RUN_ID": self.RUN[producer], "GITHUB_RUN_ATTEMPT": "2"}
        self.checkout = None
        if roles == "candidate+validator":
            self.checkout = self.temporary / "candidate"
            commit, tree = job_fixture.commit_candidate(self.mod, self.checkout)
            job_fixture.retarget(self.api, self.pull, commit, tree)
        else:
            job_fixture.seed_tested_tree(self.api, self.mod)
        self.commands = job_fixture.Commands(self.mod, self.state, self.api, self.environment)
        self.assertEqual(self.commands.run("subject", "--producer", producer, "--pr", "7", "--github-output",
                                           str(self.output)), (0, "", ""))
        self.subject = identity.read_subject(self.state)["subject"]
        self.assertEqual(self.prepare(roles)[0], 0)
        code, stdout, stderr = self.plan()
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.document = lifecycle.read_plan(
            lifecycle.open_job(runtime.build_invocation(self.mod, None, self.environment), self.state))
        self.requests = self.api.request_count
        return self.document

    def built(self):
        """Every target of the job's plan, built by the synthetic mod's candidate hook as a plain
        process: the bytes a candidate of this subject exports, in one directory."""
        box = h.Sandbox(self.temporary / "pure", protected=self.mod)
        box.subject = self.subject
        self.assertEqual(box.derive_plan(), self.document)
        for target in self.document["targets"]:
            box.build(target["id"])
        return box

    def producer(self, caller):
        from tests.helpers import ci_run_producer

        return {key: value for key, value in ci_run_producer(self.document, caller).items() if key != "upload_window"}

    def build_envelope(self, root, target_id=None):
        """The canonical Build envelope of the planned files below ``root``, sealed by run 42, attempt 2."""
        plan = self.document
        files = sorted(({**output, "size": (root / output["path"]).stat().st_size,
                         "sha256": hashlib.sha256((root / output["path"]).read_bytes()).hexdigest()}
                        for target in plan["targets"] if target_id in (None, target["id"])
                        for output in target["outputs"]), key=lambda file: file["path"])
        return {"kind": "mod-base.build.envelope", "schema_version": 1, "identity": plan["identity"],
                "plan_sha256": plan["plan_sha256"], "profile": plan["profile"], "producer": self.producer("build"),
                "scope": "complete" if target_id is None else "target", "target_id": target_id, "files": files,
                "native_reports": [file["path"] for file in files if file["role"] == "native-report"]}

    def partition(self, box, target_id, *, mutate=None):
        """One target's files with their envelope in a directory of the runner; returns both."""
        from mod_base.build_ci import adapter

        index = [target["id"] for target in self.document["targets"]].index(target_id)
        root = self.temporary / "targets" / f"target-{index}"
        for name in adapter.target_outputs(self.document, target_id):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(box.sealed_build / name, root / name)
        if mutate is not None:
            mutate(root)
        envelope = self.build_envelope(root, target_id)
        (root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        return root, envelope

    def seal_complete_build(self, box, *, mutate=None):
        """``sealed-build/`` as ``ci assemble`` leaves it: every target's partition assembled into
        the complete Build of run 42, attempt 2. ``mutate`` changes one partition before its
        envelope is written, so the Build is still exactly what its envelope says. Returns the
        complete envelope."""
        from mod_base.build_ci.exports import assemble_build_export
        from tests.helpers import ci_run_descriptor

        partitions = []
        for index, target in enumerate(self.document["targets"]):
            _, envelope = self.partition(box, target["id"], mutate=mutate if index == 1 else None)
            partitions.append({"descriptor": ci_run_descriptor(self.document, "build", "full", "target",
                                                               unit_id=target["id"], artifact_id=110 + index),
                               "envelope": envelope})
        envelope = assemble_build_export(self.temporary / "targets", partitions=partitions, plan=self.document,
                                         run_id=42, run_attempt=2, output=self.build)
        self.assertEqual(self.owner(self.build), (os.getuid(), os.getgid(), 0o700))
        return envelope

    def frozen(self, root):
        """Give a private copy the modes a freeze leaves: 0700 directories and 0600 files."""
        for path in (root, *root.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)

    def seal_partition(self, box, target_id):
        """``sealed-build/`` as ``ci worker-seal`` leaves it in a target job: the runner-private
        copy of that target's export with its envelope. Returns the envelope."""
        root, envelope = self.partition(box, target_id)
        self.assertEqual(materialize_build_export(root, self.build, plan=self.document), envelope)
        self.frozen(self.build)
        return envelope

    def seal_lane(self, box, lane_id):
        """``sealed-runtime/`` as ``ci worker-seal`` leaves it in a lane job: the runner-private
        copy of the lane's native results with their envelope, owned by the complete Build of the
        Build run. The results come from the synthetic ``run_lane``. Returns the envelope."""
        from mod_base.build_ci.runtime_exports import materialize_runtime_export
        from mod_base.build_ci.runtime_inputs import RUNTIME_VALIDATION_ROOT
        from tests.helpers import ci_run_descriptor

        box.run_lane(lane_id)
        staging = self.temporary / "lane"
        shutil.copytree(box.sealed_runtime, staging)
        roles = {".json": "native-report", ".log": "runtime-log", ".png": "screenshot"}
        plan = self.document
        lane = next(lane for lane in plan["lanes"] if lane["id"] == lane_id)
        files = [{"path": name, "lane_id": lane_id, "role": roles[Path(name).suffix],
                  "size": (staging / name).stat().st_size,
                  "sha256": hashlib.sha256((staging / name).read_bytes()).hexdigest()}
                 for name in sorted(h.files(staging))]
        envelope = {"kind": "mod-base.ci.runtime-envelope", "schema_version": 1, "identity": plan["identity"],
                    "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                    "producer": self.producer("packaged"), "scope": "lane", "lane_id": lane_id,
                    "owning_build": ci_run_descriptor(plan, "build", "full", "build"),
                    "lanes": [{"id": lane_id, "native_contract_sha256": lane["native_contract_sha256"]}],
                    "files": files}
        (staging / grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        runtime = Path(str(RUNTIME_VALIDATION_ROOT))
        self.assertEqual(materialize_runtime_export(staging, runtime, plan=plan), envelope)
        self.frozen(runtime)
        return envelope

    def validate(self, hook, *unit, output=None):
        return self.commands.run("worker-validate", "--hook", hook, *unit, "--output",
                                 str(self.upload if output is None else output))

    def tree(self, root):
        return {name: (root / name).read_bytes() for name in h.files(root)}

    def read_artifact(self, record):
        """A reader's view of the uploaded root: the verification detached from the export."""
        export, detached = self.temporary / "read-export", self.temporary / "read-validation"
        shutil.copytree(self.upload, export)
        detached.mkdir()
        for name in (grammar.CI_VALIDATION_NAME, *(report["path"] for report in record["reports"])):
            shutil.move(export / name, detached / name)
        return export, detached

    def assert_sealed_verification(self, hook, unit_id, envelope_name, input_sha256, units):
        """The upload's record is the kit's, bound to the plan, this job and the uploaded bytes."""
        from mod_base.build_ci.validation import verify_validation_export
        from mod_base.model.documents import load_document

        uploaded = self.tree(self.upload)
        record = load_document(uploaded[grammar.CI_VALIDATION_NAME], kind="mod-base.ci.validation", plan=self.document)
        self.assertEqual(uploaded[grammar.CI_VALIDATION_NAME], canonical_json(record))
        config = hashlib.sha256((self.mod / "scripts/ci/mod-base-build.json").read_bytes()).hexdigest()
        self.assertEqual({key: record[key] for key in ("hook", "unit_id", "run_id", "run_attempt",
                                                       "source_config_sha256", "input_sha256")},
                         {"hook": hook, "unit_id": unit_id, "run_id": int(self.environment["GITHUB_RUN_ID"]),
                          "run_attempt": 2, "source_config_sha256": config, "input_sha256": input_sha256})
        self.assertEqual((record["identity"], record["plan_sha256"]),
                         (self.document["identity"], self.document["plan_sha256"]))
        self.assertEqual(record["reports"], [
            {"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
             "path": unit["id"] + ".json", "size": len(uploaded[unit["id"] + ".json"]),
             "sha256": hashlib.sha256(uploaded[unit["id"] + ".json"]).hexdigest()} for unit in units])
        for unit in units:  # The reports are the synthetic verifier's own; it wrote nothing else.
            self.assertIn(f'"hook":"{hook}"'.encode(), uploaded[unit["id"] + ".json"])
            self.assertIn(f'"unit":"{unit["id"]}"'.encode(), uploaded[unit["id"] + ".json"])
        # The private sealed copy root made is exactly what went up beside the envelope.
        self.assertEqual(self.owner(self.sealed), (os.getuid(), os.getgid(), 0o700))
        self.assertEqual(self.tree(self.sealed), {name: uploaded[name] for name in (
            grammar.CI_VALIDATION_NAME, *(unit["id"] + ".json" for unit in units))})
        for leaf in self.sealed.iterdir():
            self.assertEqual(self.owner(leaf), (os.getuid(), os.getgid(), 0o600))
        export, detached = self.read_artifact(record)
        context = {key: record[key] for key in ("hook", "unit_id", "run_id", "run_attempt", "source_config_sha256",
                                                "input_sha256")}
        self.assertEqual(verify_validation_export(detached, plan=self.document, **context), record)
        self.assertEqual(self.owner(self.upload), (os.getuid(), os.getgid(), 0o700))
        self.assertEqual(sorted(uploaded), sorted([*self.tree(export), *self.tree(detached)]))
        self.assertIn(envelope_name, self.tree(export))
        return record, export

    def assert_private_to_the_runner(self, *roles):
        """No worker account reads the runner's state, the upload or what root sealed for the runner."""
        private = [self.state, self.mod, self.HOME, self.upload, self.sealed, self.root / "execution-handoff",
                   *self.root.glob("root-request-*")]
        self.assertGreaterEqual(len(private), 8)
        for role in roles:
            for path in private:
                self.assertEqual(self.as_account(role, "/usr/bin/test", "-r", str(path)), 1, (role, path))
                self.assertEqual(self.as_account(role, "/usr/bin/test", "-w", str(path)), 1, (role, path))

    def assert_read_only_for_the_validator(self, root):
        """The validator reads the sealed export it verified and can change nothing of it."""
        validator = self.account("validator")
        for path in (root, *root.rglob("*")):
            self.assertEqual(self.owner(path), (os.getuid(), validator.pw_gid, 0o750 if path.is_dir() else 0o640), path)
            self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(path)), 0, path)
            self.assertEqual(self.as_account("validator", "/usr/bin/test", "-w", str(path)), 1, path)
        leaf = next(path for path in sorted(root.rglob("*")) if path.is_file())
        for attempt in (("/usr/bin/touch", str(root / "added")), ("/usr/bin/rm", "-f", str(leaf)),
                        ("/usr/bin/chmod", "0666", str(leaf)), ("/usr/bin/mv", str(leaf), str(leaf) + ".moved")):
            self.assertEqual(self.as_account("validator", *attempt), 1, attempt)

    def assert_nothing_validated(self, *roles, granted=False):
        """A rejection: no upload, no sealed verification, every account locked and idle."""
        self.assertFalse(self.upload.exists())
        self.assertFalse(self.sealed.exists())
        self.assertEqual([path.name for path in self.temporary.iterdir() if path.name.startswith(".")], [])
        if not granted:
            self.assertEqual(self.owner(self.build), (os.getuid(), os.getgid(), 0o700))
            self.assertFalse((self.root / "root-request-grant-build-validation").exists())
        self.assertEqual(self.home(), 0o700)
        self.assert_resting(*roles)
        self.assertEqual(self.api.request_count, self.requests)  # The command asks the API nothing.

    # -- the complete Build in a job with the validator alone ----------------------------------------------

    def test_validator_job_verifies_the_assembled_build_and_writes_the_upload_directory(self):
        from mod_base.build_ci.records import bind_build_envelope
        from mod_base.model.documents import load_document
        from tests.helpers import ci_run_descriptor

        plan = self.begin("validator")
        envelope = self.seal_complete_build(self.built())
        planned = sorted(output["path"] for target in plan["targets"] for output in target["outputs"])
        sealed = self.tree(self.build)
        self.assertEqual(sorted(sealed), sorted([grammar.CI_ENVELOPE_NAME, *planned]))

        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(stdout, "[validator] synthetic verify_build: ok, 2 files\n"
                                 f"worker-validate: verify_build verified {len(planned)} sealed files; "
                                 "2 reports sealed; upload directory written\n")
        # The upload is the export root: its files, its envelope and, beside it, the record and reports.
        uploaded = self.tree(self.upload)
        self.assertEqual(sorted(uploaded), sorted([grammar.CI_ENVELOPE_NAME, grammar.CI_VALIDATION_NAME,
                                                   "1.20.1.json", "1.21.1.json", *planned]))
        self.assertEqual({name: uploaded[name] for name in sealed}, sealed)
        self.assertEqual(load_document(uploaded[grammar.CI_ENVELOPE_NAME], kind="mod-base.build.envelope",
                                       plan=plan), envelope)
        self.assertEqual(uploaded[grammar.CI_ENVELOPE_NAME], canonical_json(envelope))
        bind_build_envelope(envelope, descriptor=ci_run_descriptor(plan, "build", "full", "build"), plan=plan)
        record, export = self.assert_sealed_verification(
            "verify_build", None, grammar.CI_ENVELOPE_NAME,
            hashlib.sha256(uploaded[grammar.CI_ENVELOPE_NAME]).hexdigest(), plan["targets"])
        self.assertEqual(verify_build_export(export, plan=plan), envelope)  # What a reader verifies is unchanged.
        for path in self.upload.rglob("*"):
            self.assertEqual(self.owner(path), (os.getuid(), os.getgid(), 0o700 if path.is_dir() else 0o644), path)

        # The accounts: the validator alone, locked, with read access to what it verified and to no more.
        self.assert_resting("validator")
        self.assert_read_only_for_the_validator(self.build)
        self.assert_private_to_the_runner("validator")
        self.assertEqual(sealed, self.tree(self.build))
        self.assertEqual(sorted(command("/usr/bin/sudo", "-n", "/usr/bin/find", str(self.root / "validator-home/validation"),
                                        "-type", "f", "-printf", "%f %m %U\\n").stdout.decode().split("\n")[:-1]),
                         [f"{name} 600 {self.account('validator').pw_uid}" for name in ("1.20.1.json", "1.21.1.json")])
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (self.requests, [limits.MAX_CI_SUBJECT_REQUESTS, limits.MAX_CI_PLAN_REQUESTS]))
        self.assertEqual(self.api.mutations, [])

        # One verification per job: the directory is never replaced, and nothing is verified twice.
        before = self.tree(self.upload)
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stdout, stderr), (2, "", "mod_base: ci-lifecycle: the upload directory exists "
                                                         "already; `ci worker-validate` never replaces one\n"))
        code, stdout, stderr = self.validate("verify_build", output=self.temporary / "second-upload")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("sealed-build/ is not the private export this job has just sealed", stderr)
        self.assertFalse((self.temporary / "second-upload").exists())
        self.assertEqual(self.tree(self.upload), before)
        self.assert_resting("validator")
        self.assert_finished("validator")

    def test_an_existing_upload_directory_is_refused_before_anything_runs(self):
        self.begin("validator")
        self.seal_complete_build(self.built())
        self.upload.mkdir()
        (self.upload / "earlier").write_bytes(b"kept")
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stdout, stderr), (2, "", "mod_base: ci-lifecycle: the upload directory exists "
                                                         "already; `ci worker-validate` never replaces one\n"))
        self.assertEqual(self.tree(self.upload), {"earlier": b"kept"})
        # Nothing ran: no request for root, no execution record, and the Build is still the runner's alone.
        self.assertEqual(sorted(path.name for path in self.root.iterdir()
                                if path.name.startswith(("root-request-grant-build", "root-request-freeze",
                                                         "execution-handoff", "sealed-validation"))), [])
        self.assertEqual(self.owner(self.build), (os.getuid(), os.getgid(), 0o700))
        self.assertEqual(self.as_account("validator", "/usr/bin/test", "-r", str(self.build)), 1)
        self.assertEqual(command("/usr/bin/sudo", "-n", "/usr/bin/test", "-e",
                                 str(self.root / "validator-home/validation"), accepted=(0, 1)).returncode, 1)
        self.assert_resting("validator")
        self.upload.joinpath("earlier").unlink()
        self.upload.rmdir()
        self.assertEqual(self.validate("verify_build")[0], 0)  # The refusal spent nothing of the job.
        self.assert_finished("validator")

    def test_a_hook_of_another_kind_of_job_or_for_another_unit_is_refused_before_anything_runs(self):
        self.begin("validator")
        self.seal_complete_build(self.built())
        refusals = [(("verify_target", "--unit", "1.20.1"), "ci-lifecycle: verify_target runs in a job that allocated "
                                                            "candidate and validator; this job allocated validator"),
                    (("verify_runtime", "--unit", "fabric-1.20.1"), "this job allocated validator"),
                    (("verify_build", "--unit", "1.20.1"), "verify_build runs for no unit"),
                    (("verify_target", "--unit", "1.19.4"), "outside the protected plan"),
                    (("verify_target",), "outside the protected plan")]
        for arguments, message in refusals:
            code, stdout, stderr = self.validate(*arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(message, stderr)
                self.assertEqual(stderr.count("\n"), 1)
            self.assert_nothing_validated("validator")
        self.assertEqual(self.validate("verify_build")[0], 0)
        self.assert_finished("validator")

    def test_a_build_the_synthetic_verifier_rejects_is_never_uploaded(self):
        # One byte of a production JAR differs from what its manifest says. The envelope was
        # written afterwards, so the kit finds the Build exactly as sealed: only the mod's own
        # verification can tell.
        def tamper(root):
            jar = next(path for path in sorted(root.rglob("*.jar")) if "harness" not in path.parts)
            data = bytearray(jar.read_bytes())
            data[-1] ^= 0x01
            jar.write_bytes(bytes(data))

        self.begin("validator")
        self.seal_complete_build(self.built(), mutate=tamper)
        self.assertEqual(verify_build_export(self.build, plan=self.document)["scope"], "complete")
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stderr), (2, "mod_base: ci-worker: worker dispatcher returned failure\n"), stdout)
        self.assertRegex(stdout, r"^\[validator\] synthetic verify_build rejected: .+\n$")
        self.assert_nothing_validated("validator", granted=True)
        self.assertFalse((self.root / "root-request-freeze-build-validation").exists())
        self.assertFalse((self.root / "execution-handoff").exists())
        self.assert_finished("validator")

    def test_a_sealed_build_that_is_not_its_envelope_never_reaches_the_validator(self):
        self.begin("validator")
        envelope = self.seal_complete_build(self.built())
        leaf = self.build / envelope["files"][0]["path"]
        original = leaf.read_bytes()
        cases = {"changed byte": lambda: leaf.write_bytes(bytes([original[0] ^ 0x01]) + original[1:]),
                 "extra file": lambda: (self.build / "unplanned.txt").write_bytes(b"x"),
                 "missing file": lambda: leaf.unlink()}
        for label, mutate in cases.items():
            mutate()
            code, stdout, stderr = self.validate("verify_build")
            with self.subTest(case=label):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("frozen export differs from its exact file inventory", stderr)
            self.assert_nothing_validated("validator")
            leaf.write_bytes(original)
            (self.build / "unplanned.txt").unlink(missing_ok=True)
        os.chmod(self.build, 0o755)  # Not private any more: whoever could have changed it.
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("sealed-build/ is neither the runner's private export nor granted", stderr)
        os.chmod(self.build, 0o700)
        self.assert_nothing_validated("validator")
        self.assertEqual(self.validate("verify_build")[0], 0)
        self.assert_finished("validator")

    def test_a_job_without_a_sealed_build_has_nothing_to_verify(self):
        self.begin("validator")
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "mod_base: ci-worker: cannot read the sealed Build export\n")
        self.assertFalse(self.upload.exists())
        self.assert_resting("validator")
        self.assert_finished("validator")

    # -- hook faults ------------------------------------------------------------------------------------

    def fault(self, mode, *, error, log=None, timeout=None, frozen=False):
        """A ``verify_build`` that misbehaves is a clean rejection: one error line, no upload, the
        validator locked with nothing of its own running, and the sweep still restores the home."""
        self.begin("validator", faults=[{"hook": "verify_build", "unit": None, "mode": mode}], timeout=timeout)
        self.seal_complete_build(self.built())
        started = time.monotonic()
        code, stdout, stderr = self.validate("verify_build")
        self.assertEqual(code, 2, stdout + stderr)
        self.assertRegex(stderr, error)
        self.assertEqual(stderr.count("\n"), 1)
        if log is not None:
            self.assertEqual(stdout, log)
        self.assertLess(time.monotonic() - started, 60)
        self.assert_nothing_validated("validator", granted=True)
        # Root is asked to freeze only what a hook that ran to its end left behind.
        self.assertEqual((self.root / "root-request-freeze-build-validation").exists(), frozen)
        self.assert_read_only_for_the_validator(self.build)
        self.assert_finished("validator")

    def test_failing_verifier_is_rejected_with_its_log(self):
        self.fault("fail", error=r"^mod_base: ci-worker: worker dispatcher returned failure\n$",
                   log="[validator] synthetic verify_build rejected: the release inventory requests this failure\n")

    def test_hanging_verifier_is_killed_at_the_protected_timeout(self):
        self.fault("hang", timeout=3, error=r"^mod_base: ci-worker: worker execution timed out\n$", log="")

    def test_verifier_that_leaves_an_orphan_process_is_rejected_and_the_orphan_is_killed(self):
        self.fault("orphan", error=r"^mod_base: ci-worker: worker dispatcher left a process behind\n$")

    def test_verifier_that_writes_an_extra_file_is_rejected_by_root(self):
        self.fault("extra", frozen=True, log="[validator] synthetic verify_build: ok, 3 files\n",
                   error=r"^mod_base: ci-worker: root operation freeze-build-validation failed with exit 2: .*"
                         r"exactly one `<unit id>\.json` report per unit and nothing else")

    def test_verifier_that_omits_a_report_is_rejected_by_root(self):
        self.fault("missing", frozen=True, log="[validator] synthetic verify_build: ok, 2 files\n",
                   error=r"^mod_base: ci-worker: root operation freeze-build-validation failed with exit 2: .*"
                         r"exactly one `<unit id>\.json` report per unit and nothing else")

    # -- a target's partition and a lane, in jobs with both accounts -------------------------------------

    def test_target_job_verifies_its_frozen_partition_with_both_accounts_locked(self):
        plan = self.begin("candidate+validator")
        box = self.built()
        envelope = self.seal_partition(box, "1.20.1")
        target = plan["targets"][0]
        self.assertEqual((target["id"], envelope["scope"], envelope["target_id"]), ("1.20.1", "target", "1.20.1"))
        # The partition of one target never answers for another, and never for the complete Build.
        for arguments, message in ((("verify_target", "--unit", "1.21.1"), "exactly its own frozen partition"),
                                   (("verify_build",), "this job allocated candidate and validator")):
            code, stdout, stderr = self.validate(*arguments)
            self.assertEqual((code, stdout), (2, ""), arguments)
            self.assertIn(message, stderr)
            self.assert_nothing_validated("candidate", "validator")

        code, stdout, stderr = self.validate("verify_target", "--unit", "1.20.1")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(stdout, "[validator] synthetic verify_target 1.20.1: ok, 1 files\n"
                                 f"worker-validate: verify_target 1.20.1 verified {len(target['outputs'])} sealed "
                                 "files; 1 report sealed; upload directory written\n")
        uploaded = self.tree(self.upload)
        self.assertEqual(sorted(uploaded), sorted([grammar.CI_ENVELOPE_NAME, grammar.CI_VALIDATION_NAME, "1.20.1.json",
                                                   *(output["path"] for output in target["outputs"])]))
        self.assertEqual(uploaded[grammar.CI_ENVELOPE_NAME], canonical_json(envelope))
        _, export = self.assert_sealed_verification(
            "verify_target", "1.20.1", grammar.CI_ENVELOPE_NAME,
            hashlib.sha256(canonical_json(envelope)).hexdigest(), [target])
        self.assertEqual(verify_build_export(export, plan=plan), envelope)
        self.assert_resting("candidate", "validator")
        self.assert_read_only_for_the_validator(self.build)
        self.assert_private_to_the_runner("candidate", "validator")
        for path in (self.build, self.root / "validation-input", self.root / "controller"):
            self.assertEqual(self.as_account("candidate", "/usr/bin/test", "-r", str(path)), 1, path)
        self.assert_finished("candidate", "validator")

    def test_lane_job_verifies_its_results_against_the_owning_build(self):
        self.lane_job(handed_over=False)

    def test_lane_job_takes_an_owning_build_an_earlier_step_already_handed_to_the_validator(self):
        self.lane_job(handed_over=True)

    def lane_job(self, *, handed_over):
        """A lane's results are verified against the complete Build of the Build run. That Build is
        the runner's private copy, or a step before this one has already handed it to the
        validator: each operation has one request per job, so the command must not ask again."""
        from mod_base import runtime as invocations
        from mod_base.build_ci.root_request import request_build_validation_grant
        from mod_base.build_ci.runtime_exports import verify_runtime_export
        from mod_base.build_ci.runtime_inputs import RUNTIME_VALIDATION_ROOT, _context

        plan = self.begin("candidate+validator", producer="packaged")
        box = self.built()
        build = self.seal_complete_build(box)  # The Build of run 42, as `ci fetch-build` materialises it.
        lane_id = "forge-1.20.1"
        envelope = self.seal_lane(box, lane_id)
        lane = next(lane for lane in plan["lanes"] if lane["id"] == lane_id)
        runtime = Path(str(RUNTIME_VALIDATION_ROOT))
        self.assertEqual((envelope["producer"]["run_id"], build["producer"]["run_id"]), (43, 42))
        if handed_over:
            job = lifecycle.open_job(invocations.build_invocation(self.mod, None, self.environment), self.state)
            worker = lifecycle.open_worker(job)
            lifecycle._root(job, worker.python, "grant-build-validation", request_build_validation_grant(
                boundary=worker.boundary, validator=worker.validator, plan=plan, envelope=build))
            self.assert_read_only_for_the_validator(self.build)
        refusals = [(("verify_runtime", "--unit", "fabric-1.20.1"), "requires the exact frozen lane"),
                    (("verify_target", "--unit", "1.20.1"),
                     "not the private export this job has just sealed" if handed_over
                     else "exactly its own frozen partition")]
        for arguments, message in refusals:
            code, stdout, stderr = self.validate(*arguments)
            self.assertEqual((code, stdout), (2, ""), arguments)
            self.assertIn(message, stderr)
            self.assert_nothing_validated("candidate", "validator", granted=handed_over)
            self.assertEqual(self.owner(runtime), (os.getuid(), os.getgid(), 0o700))
            self.assertFalse((self.root / "root-request-grant-runtime-validation").exists())

        code, stdout, stderr = self.validate("verify_runtime", "--unit", lane_id)
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(stdout, f"[validator] synthetic verify_runtime {lane_id}: ok, 1 files\n"
                                 f"worker-validate: verify_runtime {lane_id} verified {len(envelope['files'])} sealed "
                                 "files; 1 report sealed; upload directory written\n")
        # The lane's upload is its own results: the owning Build is read, verified against and left out.
        uploaded = self.tree(self.upload)
        self.assertEqual(sorted(uploaded), sorted([grammar.CI_RUNTIME_ENVELOPE_NAME, grammar.CI_VALIDATION_NAME,
                                                   f"{lane_id}.json", *(file["path"] for file in envelope["files"])]))
        self.assertEqual(uploaded[grammar.CI_RUNTIME_ENVELOPE_NAME], canonical_json(envelope))
        self.assertTrue(all(name.startswith(f"lanes/{lane_id}/") for name in (file["path"] for file in envelope["files"])))
        _, export = self.assert_sealed_verification(
            "verify_runtime", lane_id, grammar.CI_RUNTIME_ENVELOPE_NAME,
            _context(plan, build, envelope, lane_id=lane_id, run_id=43, run_attempt=2)[0], [lane])
        self.assertEqual(verify_runtime_export(export, plan=plan), envelope)
        self.assert_resting("candidate", "validator")
        for root in (self.build, runtime):
            self.assert_read_only_for_the_validator(root)
            self.assertEqual(self.as_account("candidate", "/usr/bin/test", "-r", str(root)), 1, root)
        self.assert_private_to_the_runner("candidate", "validator")
        self.assertEqual(sorted(path.name for path in self.root.glob("root-request-*validation")),
                         ["root-request-freeze-runtime-validation", "root-request-grant-build-validation",
                          "root-request-grant-runtime-validation"])
        self.assert_finished("candidate", "validator")


if __name__ == "__main__":
    unittest.main()
