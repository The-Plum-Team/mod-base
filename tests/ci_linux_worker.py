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

from mod_base.build_ci import worker
from mod_base.build_ci.worker import (WORKER_ACCOUNTS, WORKER_ROOT, WorkerExecutionError,
                                      allocate_worker_account, authenticate_worker_account,
                                      prepare_worker_boundary, terminate_worker)
from tests.helpers import ci_plan
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


def command(*args, accepted=(0,), cwd=None):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20, env=HOST_ENV, check=False, cwd=cwd)
    if result.returncode not in accepted:
        raise AssertionError(f"Linux fixture command {args[0]} failed ({result.returncode})")
    return result


class LinuxWorkerTests(unittest.TestCase):
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
        for role, name in WORKER_ACCOUNTS.items():
            account = allocate_worker_account(role)
            self.accounts.append((role, account.uid, account.gid))
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

    def test_existing_boundary_is_not_adopted_or_replaced(self):
        with self.assertRaises(MbError):
            prepare_worker_boundary(runner_environment="github-hosted")
        self.assertEqual(stat.S_IMODE(self.boundary.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o711)

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
        proof = inspect_worker_toolchains(boundary=self.host_boundary, roots=(sys.base_prefix,))
        self.assertGreater(proof.files, 0)
        self.assertGreater(proof.entries, proof.files)
        _execution_tool_paths(proof, self.host_boundary, sys.executable, sys.base_prefix)
        with self.assertRaisesRegex(MbError, "regular file"):
            _execution_tool_paths(proof, self.host_boundary, sys.base_prefix, None)
        with self.assertRaisesRegex(MbError, "JAVA_HOME"):
            _execution_tool_paths(proof, self.host_boundary, sys.executable, sys.executable)
        result = self.run_dispatcher("print('readonly tools admitted',flush=True)\n", tools=proof)
        self.assertIn(b"readonly tools admitted", result.log)

    def test_privileged_kit_copy_is_private_digest_bound_and_imports_without_path_changes(self):
        # The authenticated release/pin guard is separate: this fixture proves actual byte,
        # ownership and isolated-import mechanics of the trusted CI checkout, not release authority.
        from mod_base.pin import kit_tree_digest
        from mod_base.build_ci.installation import PRIVILEGED_KIT_ROOT
        kit = Path(__file__).resolve().parents[1]
        digest = kit_tree_digest(kit)
        body = ("import importlib.util,json,os,pathlib,sys\n"
                "original=tuple(sys.path)\n"
                "assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode\n"
                "def load(root):\n"
                "    spec=importlib.util.spec_from_file_location('mod_base',root/'__init__.py',"
                "submodule_search_locations=[str(root)])\n"
                "    module=importlib.util.module_from_spec(spec)\n"
                "    sys.modules['mod_base']=module\n    spec.loader.exec_module(module)\n"
                f"kit=pathlib.Path({str(kit)!r})\n"
                "guard_spec=importlib.util.spec_from_file_location('private_kit_guard',kit/'tools/ci_privileged_bootstrap.py')\n"
                "guard=importlib.util.module_from_spec(guard_spec)\nguard_spec.loader.exec_module(guard)\n"
                "assert not any(n=='mod_base' or n.startswith('mod_base.') for n in sys.modules)\n"
                "assert tuple(sys.path)==original\nload(kit/'src/mod_base')\n"
                "from mod_base.build_ci.host import HostBoundary\n"
                "from mod_base.build_ci.installation import install_privileged_kit,authenticate_privileged_kit\n"
                "from mod_base.build_ci.installation_record import record_privileged_kit_installation,read_privileged_kit_installation\n"
                "from mod_base.build_ci.bootstrap_installation import install_privileged_bootstrap,authenticate_privileged_bootstrap,PRIVILEGED_BOOTSTRAP_ROOT\n"
                "from mod_base.runtime import build_invocation\n"
                "from mod_base.errors import MbError\n"
                "inv=build_invocation(kit/'tests/fixtures/mods/qs_like',None,"
                "{'MOD_BASE_KIT_SHA':'a'*40},check_repository=False,root=kit)\n"
                f"boundary={self.host_boundary!r}\n"
                f"installed=install_privileged_kit(inv,boundary=boundary,expected_digest={digest!r})\n"
                "try:\n"
                f"    install_privileged_kit(inv,boundary=boundary,expected_digest={digest!r})\n"
                "except MbError:\n    pass\n"
                "else:\n    raise AssertionError('existing private installation was reused')\n"
                "authenticate_privileged_kit(installed,boundary=boundary)\n"
                f"previous=pathlib.Path({str(PRIVILEGED_KIT_ROOT)!r})\n"
                "previous.rename(previous.with_name('privileged-kit-original'))\n"
                f"replacement=install_privileged_kit(inv,boundary=boundary,expected_digest={digest!r})\n"
                "assert replacement.digest==installed.digest and replacement.inode!=installed.inode\n"
                "try:\n    authenticate_privileged_kit(installed,boundary=boundary)\n"
                "except MbError as error:\n    assert 'root identity changed' in str(error)\n"
                "else:\n    raise AssertionError('identical-byte root replacement retained stale authority')\n"
                "authenticate_privileged_kit(replacement,boundary=boundary)\n"
                "program=install_privileged_bootstrap(inv,boundary=boundary,installation=replacement)\n"
                "authenticate_privileged_bootstrap(program,boundary=boundary,installation=replacement)\n"
                "try:\n    install_privileged_bootstrap(inv,boundary=boundary,installation=replacement)\n"
                "except MbError:\n    pass\n"
                "else:\n    raise AssertionError('private program was replaced')\n"
                "record_privileged_kit_installation(replacement,boundary=boundary)\n"
                "assert read_privileged_kit_installation(boundary=boundary)==replacement\n"
                "try:\n    record_privileged_kit_installation(replacement,boundary=boundary)\n"
                "except MbError:\n    pass\n"
                "else:\n    raise AssertionError('existing installation record was overwritten')\n"
                "from mod_base.build_ci.installation_record import PRIVILEGED_KIT_RECORD_ROOT\n"
                "from mod_base.model import grammar,limits\n"
                "from mod_base.model.canonical import canonical_json\n"
                "record_root=pathlib.Path(str(PRIVILEGED_KIT_RECORD_ROOT))\n"
                "leaf=record_root/grammar.CI_KIT_INSTALLATION_NAME\n"
                "saved=leaf.read_bytes()\n"
                "approved=dict(expected_sha=replacement.kit_sha,expected_version=replacement.kit_version,"
                "expected_digest=replacement.digest)\n"
                "def denied(label):\n"
                "    try:\n        read_privileged_kit_installation(boundary=boundary)\n"
                "    except MbError:\n        pass\n"
                "    else:\n        raise AssertionError('altered installation record admitted: '+label)\n"
                "    try:\n        guard.authenticate_fixed_kit(**approved)\n"
                "    except guard.BootstrapError:\n        pass\n"
                "    else:\n        raise AssertionError('independent guard admitted altered record: '+label)\n"
                "for mode in (0o644,0o400):\n"
                "    leaf.chmod(mode)\n"
                "    try:\n        denied('leaf mode')\n"
                "    finally:\n        leaf.chmod(0o600)\n"
                "record_root.chmod(0o755)\n"
                "try:\n    denied('directory mode')\n"
                "finally:\n    record_root.chmod(0o700)\n"
                "os.chown(leaf,boundary.uid,boundary.gid)\n"
                "try:\n    denied('runner-owned leaf')\n"
                "finally:\n    os.chown(leaf,0,0)\n"
                "extra=record_root/'unexpected'\nextra.write_bytes(b'inert')\n"
                "try:\n    denied('extra entry')\n"
                "finally:\n    extra.unlink()\n"
                "held=record_root.with_name('held-installation-record')\nleaf.rename(held)\n"
                "try:\n"
                "    denied('missing leaf')\n"
                "    for kind in ('symlink','hardlink','fifo'):\n"
                "        if kind=='symlink':\n            leaf.symlink_to(held)\n"
                "        elif kind=='hardlink':\n            os.link(held,leaf)\n"
                "        else:\n            os.mkfifo(leaf,0o600)\n"
                "        try:\n            denied(kind)\n"
                "        finally:\n            leaf.unlink()\n"
                "finally:\n    held.rename(leaf)\n"
                "changed=json.loads(saved)\nchanged['inode']+=1\n"
                "for raw in (saved+b' ',b'{\"kind\":1,\"kind\":2}',b'{\"value\":NaN}',"
                "b'\\xff',b' '*(limits.MAX_CI_KIT_INSTALL_RECORD_BYTES+1),canonical_json(changed)):\n"
                "    leaf.write_bytes(raw)\n"
                "    try:\n        denied('record bytes or root identity')\n"
                "    finally:\n        leaf.write_bytes(saved)\n"
                "assert read_privileged_kit_installation(boundary=boundary)==replacement\n"
                "for name in list(sys.modules):\n"
                "    if name=='mod_base' or name.startswith('mod_base.'):\n        del sys.modules[name]\n"
                "program_path=pathlib.Path(str(PRIVILEGED_BOOTSTRAP_ROOT))/grammar.CI_BOOTSTRAP_PROGRAM_NAME\n"
                "guard_spec=importlib.util.spec_from_file_location('installed_private_kit_guard',program_path)\n"
                "guard=importlib.util.module_from_spec(guard_spec)\nguard_spec.loader.exec_module(guard)\n"
                "guard.load_fixed_kit(**approved)\n"
                "assert 'mod_base' in sys.modules\n"
                f"root=pathlib.Path({str(PRIVILEGED_KIT_ROOT)!r})/'src/mod_base'\n"
                "from mod_base.build_ci.handoff import freeze_handed_off_build_validation\n"
                "assert callable(freeze_handed_off_build_validation) and tuple(sys.path)==original\n"
                "assert 'PIL' not in sys.modules\n"
                "for name,module in sys.modules.items():\n"
                "    if name=='mod_base' or name.startswith('mod_base.'):\n"
                "        assert pathlib.Path(module.__file__).is_relative_to(root)\n"
                "print('private isolated kit admitted',flush=True)\n")
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", body, cwd=self.root)
        self.assertIn(b"private isolated kit admitted", result.stdout)
        root = Path(str(PRIVILEGED_KIT_ROOT))
        from mod_base.build_ci.installation_record import PRIVILEGED_KIT_RECORD_ROOT
        record_root = Path(str(PRIVILEGED_KIT_RECORD_ROOT))
        from mod_base.build_ci.bootstrap_installation import PRIVILEGED_BOOTSTRAP_ROOT
        program_root = Path(str(PRIVILEGED_BOOTSTRAP_ROOT))
        self.assertEqual((program_root.stat().st_uid, program_root.stat().st_gid, stat.S_IMODE(program_root.stat().st_mode)),
                         (0, 0, 0o700))
        self.assertEqual((record_root.stat().st_uid, record_root.stat().st_gid, stat.S_IMODE(record_root.stat().st_mode)),
                         (0, 0, 0o700))
        self.assertEqual((root.stat().st_uid, root.stat().st_gid, stat.S_IMODE(root.stat().st_mode)), (0, 0, 0o700))
        for role in WORKER_ACCOUNTS:
            for path in (root, root / "src/mod_base/__init__.py", record_root,
                         record_root / grammar.CI_KIT_INSTALLATION_NAME, program_root,
                         program_root / grammar.CI_BOOTSTRAP_PROGRAM_NAME):
                self.assertEqual(self.as_worker(role, "/usr/bin/test", "-r", str(path), accepted=(0, 1)).returncode, 1)

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
                                                  materialize_controller_sources)
        from mod_base.build_ci.inputs import (VALIDATOR_INPUT_ROOT, execute_frozen_build_validator,
                                              execute_frozen_target_validator, materialize_validation_plan)
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
                  f"path=root/{grammar.CI_VALIDATION_NAME!r}\npath.write_bytes(encode(receipt))\npath.chmod(0o600)\n"
                  "print('inert-validator-hook',flush=True)\n").encode()
        plan, api, _, protected = ControllerSourceTests().fixture(source_data=native, empty_module=True)
        plan = executing_plan
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        materialize_controller_sources(Path(str(CONTROLLER_VALIDATION_ROOT)), sources=sources, identity=plan["identity"])
        materialize_validation_plan(Path(str(VALIDATOR_INPUT_ROOT)), plan=plan)
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
                 "from mod_base.runtime import build_invocation\n"
                 "from mod_base.build_ci.installation import install_privileged_kit\n"
                 "from mod_base.build_ci.installation_record import record_privileged_kit_installation\n"
                 "from mod_base.build_ci.bootstrap_installation import install_privileged_bootstrap\n"
                 f"inv=build_invocation(Path({str(kit / 'tests/fixtures/mods/qs_like')!r}),None,"
                 f"{{'MOD_BASE_KIT_SHA':{plan['identity']['kit']['sha']!r}}},check_repository=False,root=Path({str(kit)!r}))\n"
                 f"installed=install_privileged_kit(inv,boundary={self.host_boundary!r},expected_digest={digest!r})\n"
                 f"record_privileged_kit_installation(installed,boundary={self.host_boundary!r})\n"
                 f"install_privileged_bootstrap(inv,boundary={self.host_boundary!r},installation=installed)\n"
                 f"prepare_controller_validation(boundary={self.host_boundary!r},validator={validator!r},"
                 f"sources={sources!r},identity={plan['identity']!r})\n"
                 f"prepare_build_validation(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n"
                 f"prepare_validation_plan(boundary={self.host_boundary!r},validator={validator!r},plan={plan!r})\n")
        setup = "from pathlib import Path\n" + setup
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", setup, cwd=self.root)
        tools = inspect_worker_toolchains(boundary=self.host_boundary, roots=(sys.base_prefix,))
        execute = execute_frozen_target_validator if hook == "verify_target" else execute_frozen_build_validator
        arguments = {"target_id": unit_id} if hook == "verify_target" else {}
        bound = execute(boundary=self.host_boundary, validator=validator, **arguments,
                    sources=sources, tools=tools, plan=plan, envelope=envelope,
                    python=sys.executable, java_home=None, run_id=42, run_attempt=2)
        result = bound.execution
        from mod_base.build_ci.handoff import EXECUTION_HANDOFF_ROOT, record_build_validation_execution
        nonce = record_build_validation_execution(boundary=self.host_boundary, sources=sources,
                    bound=bound, plan=plan, envelope=envelope, run_id=42, run_attempt=2)
        from mod_base.build_ci.root_request import ROOT_REQUEST_ROOT, record_root_freeze_request
        from mod_base.runtime import build_invocation
        invocation = build_invocation(kit / "tests/fixtures/mods/qs_like", None,
            {"MOD_BASE_KIT_SHA":plan["identity"]["kit"]["sha"], "GITHUB_REPOSITORY":plan["identity"]["repository"],
             "GITHUB_SHA":plan["identity"]["controller_sha"]},check_repository=False,root=kit)
        entry_nonce = record_root_freeze_request(invocation,boundary=self.host_boundary,validator=validator,
            sources=sources,plan=plan,envelope=envelope,run_id=42,run_attempt=2,execution_nonce=nonce)
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
        program_path = WORKER_ROOT / "privileged-bootstrap" / grammar.CI_BOOTSTRAP_PROGRAM_NAME
        # Exercise the installed closed process entry, not generated root code. Configuration
        # is data from the protected checkout; only the admitted copied kit is imported.
        entry_arguments = (
            "--kit-sha", plan["identity"]["kit"]["sha"],
            "--kit-version", plan["identity"]["kit"]["version"], "--kit-digest", digest,
            "--repository", plan["identity"]["repository"], "--controller-sha", plan["identity"]["controller_sha"],
            "--controller-root", str(kit / "tests/fixtures/mods/qs_like"),
            "--runner-uid", str(self.host_boundary.uid), "--runner-gid", str(self.host_boundary.gid),
            "--home-device", str(self.host_boundary.device), "--home-inode", str(self.host_boundary.inode),
            "--home-original-mode", str(self.host_boundary.original_mode), "--nonce", entry_nonce)
        command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", str(program_path),
                *entry_arguments, cwd=self.root)
        frozen = Path(str(SEALED_VALIDATION_ROOT))
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
            self.assertEqual(probe("/usr/bin/test", "-r", str(ROOT_REQUEST_ROOT / grammar.CI_ROOT_REQUEST_NAME),
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
        rejected("private protected-runner-owned")
        leaf.chmod(0o600)
        root.chmod(0o750)
        rejected("private protected-runner-owned")
        root.chmod(0o700)
        duplicate = self.root / "execution-hardlink-fixture"
        os.link(leaf, duplicate)
        rejected("linked, empty or oversized")
        duplicate.unlink()
        leaf.unlink()
        leaf.symlink_to(self.private)
        rejected("private protected-runner-owned")
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
        rejected("private protected-runner-owned")
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
        from mod_base.build_ci.inputs import VALIDATOR_INPUT_ROOT, materialize_validation_plan
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
        materialize_validation_plan(Path(str(VALIDATOR_INPUT_ROOT)), plan=plan)
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

    def test_execution_reaps_an_orphan_holding_stdout_after_dispatcher_success(self):
        result = self.run_dispatcher(
            "import os,time\n"
            "if os.fork() == 0:\n    time.sleep(300)\n    os._exit(0)\n"
            "print('native-success', flush=True)\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"native-success", result.log)
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


class LinuxToolBytesTests(unittest.TestCase):
    """Real descriptor byte checks only; temporary fixtures do not enroll an interpreter."""

    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real tool byte descriptor tests require Linux")
        directory=tempfile.TemporaryDirectory(prefix="mod-base-tool-bytes-")
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.leaf=self.root/"tool"

    def digest(self):
        from mod_base.build_ci.toolchain import _stamp, _tool_file_digest
        return _tool_file_digest(str(self.leaf),_stamp(self.leaf.lstat()))

    def test_real_empty_and_multichunk_tool_bytes_have_exact_digest_without_descriptor_leaks(self):
        before=set(os.listdir("/proc/self/fd"))
        for data in (b"",b"inert tool bytes"*(limits.CI_PROCESS_READ_BYTES//8)):
            self.leaf.write_bytes(data)
            self.assertEqual(self.digest(),hashlib.sha256(data).hexdigest())
        self.assertEqual(set(os.listdir("/proc/self/fd")),before)

    def test_real_symlink_hardlink_and_fifo_are_rejected_without_blocking_or_leaking(self):
        from mod_base.build_ci.toolchain import _stamp, _tool_file_digest
        target=self.root/"target";target.write_bytes(b"inert")
        before=set(os.listdir("/proc/self/fd"))
        for kind in ("symlink","hardlink","fifo"):
            if kind=="symlink":self.leaf.symlink_to(target)
            elif kind=="hardlink":os.link(target,self.leaf)
            else:os.mkfifo(self.leaf)
            try:
                with self.subTest(kind=kind),self.assertRaises(MbError):
                    _tool_file_digest(str(self.leaf),_stamp(self.leaf.lstat()))
            finally:self.leaf.unlink()
        self.assertEqual(set(os.listdir("/proc/self/fd")),before)

    def test_real_named_replacement_and_open_file_mutation_during_read_are_rejected(self):
        from mod_base.build_ci import toolchain
        original_read=os.read
        for kind in ("replace","mutate"):
            self.leaf.write_bytes(b"inert original")
            expected=toolchain._stamp(self.leaf.stat())
            changed=False
            def reading(descriptor,size):
                nonlocal changed
                data=original_read(descriptor,size)
                if data and not changed:
                    changed=True
                    if kind=="replace":
                        replacement=self.root/"replacement"
                        replacement.write_bytes(b"inert original")
                        os.replace(replacement,self.leaf)
                    else:self.leaf.write_bytes(b"other modified")
                return data
            before=set(os.listdir("/proc/self/fd"))
            with self.subTest(kind=kind),patch.object(toolchain.os,"read",side_effect=reading),self.assertRaises(MbError):
                toolchain._tool_file_digest(str(self.leaf),expected)
            self.assertTrue(changed)
            self.assertEqual(set(os.listdir("/proc/self/fd")),before)


    def test_real_full_byte_fence_rejects_changes_around_mocked_dispatch(self):
        from mod_base.build_ci import toolchain
        from mod_base.build_ci.worker import WorkerResult
        from types import SimpleNamespace
        from contextlib import ExitStack
        if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or Path.home() != Path("/home/runner"):
            raise AssertionError("full byte fence component requires hosted runner home")
        # No actual dispatcher or account lifecycle: those outer admissions are explicit seams.
        # Authored data supplies expected file hashes before any tool-file inspection.
        data={"python":b"inert fixture executable", "module.py":b"inert fixture import"}
        with tempfile.TemporaryDirectory(prefix="mod-base-byte-fence-",dir=Path.home()) as directory:
            base=Path(directory);root=base/"tools";root.mkdir(mode=0o755)
            for name,content in data.items():
                (root/name).write_bytes(content);(root/name).chmod(0o755 if name=="python" else 0o644)
            paths=sorted({str(path) for path in (root,*root.parents)}|{str(root/name) for name in data})
            digest=hashlib.sha256(canonical_json({"format":"mod-base.tool-bytes-v1","roots":[str(root)]}))
            for path in paths:
                mode=stat.S_IMODE(Path(path).lstat().st_mode)
                row={"path":path,"mode":mode}
                if Path(path).name in data and Path(path).parent==root:
                    content=data[Path(path).name]
                    row.update(type="file",size=len(content),sha256=hashlib.sha256(content).hexdigest())
                else:
                    self.assertIn(mode,(0o700,0o755));row.update(type="directory")
                digest.update(canonical_json(row))
            expected="sha256:"+digest.hexdigest()
            boundary=SimpleNamespace(uid=os.getuid());account=WorkerAccount("worker",2001,2001,"/fixture/private")
            with ExitStack() as stack:
                stack.enter_context(patch.object(toolchain,"TOOL_INSTALL_PREFIXES",(str(base),)))
                stack.enter_context(patch.object(toolchain,"authenticate_host_boundary"))
                stack.enter_context(patch.object(toolchain,"authenticate_worker_account",return_value=account))
                terminate=stack.enter_context(patch.object(toolchain,"terminate_worker"))
                launch=stack.enter_context(patch.object(toolchain,"execute_isolated_worker",return_value=WorkerResult(0,b"",False)))
                metadata=toolchain.inspect_worker_toolchains(boundary=boundary,roots=(str(root),))
                proof=toolchain.authenticate_toolchain_bytes(metadata,boundary=boundary,expected_digest=expected)
                def execute():
                    return toolchain.execute_byte_fenced_worker(account,boundary=boundary,tools=proof,
                        expected_digest=expected,command=(),python=str(root/"python"),java_home=None,
                        identity={},run_id=1,run_attempt=1,values={},timeout_seconds=60)
                before=set(os.listdir("/proc/self/fd"))
                self.assertEqual(execute(),WorkerResult(0,b"",False));launch.assert_called_once();terminate.assert_not_called()
                def mutate(*args,**kwargs):
                    (root/"module.py").write_bytes(b"changed fixture data")
                    return WorkerResult(0,b"",False)
                launch.side_effect=mutate
                with self.assertRaises(MbError):execute()
                terminate.assert_called_once_with(account)
                self.assertEqual(set(os.listdir("/proc/self/fd")),before)


class LinuxValidationPlanTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux":
            raise AssertionError("real protected plan input tests require Linux")
        directory = tempfile.TemporaryDirectory(prefix="mod-base-validation-plan-")
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.output = self.base / "input"

    def test_exact_private_plan_copy_and_existing_output_preservation(self):
        from mod_base.build_ci.inputs import materialize_validation_plan, verify_validation_plan
        plan = ci_plan()
        self.assertEqual(materialize_validation_plan(self.output, plan=plan), plan)
        self.assertEqual(verify_validation_plan(self.output, plan=plan), plan)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual((self.output / grammar.CI_PLAN_NAME).read_bytes(), canonical_json(plan))
        with self.assertRaises(MbError):
            materialize_validation_plan(self.output, plan=plan)
        self.assertEqual(verify_validation_plan(self.output, plan=plan), plan)

    def test_changed_plan_links_and_undeclared_import_files_are_refused(self):
        from mod_base.build_ci.inputs import materialize_validation_plan, verify_validation_plan
        plan = ci_plan()
        materialize_validation_plan(self.output, plan=plan)
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
        from mod_base.build_ci.inputs import materialize_validation_plan
        with patch("mod_base.build_ci.inputs.verify_validation_plan", side_effect=MbError("stage mismatch")), \
                self.assertRaises(MbError):
            materialize_validation_plan(self.output, plan=ci_plan())
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
        self.receipt = ci_validation()
        self.context = {"plan": ci_plan(), **{key: self.receipt[key] for key in
                        ("hook", "unit_id", "run_id", "run_attempt", "source_config_sha256", "input_sha256")}}
        for report in self.receipt["reports"]:
            path = self.root / report["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json({"fixture_unit": report["unit_id"]}))
        (self.root / grammar.CI_VALIDATION_NAME).write_bytes(canonical_json(self.receipt))
        self.output = self.base / "sealed"

    def test_copy_has_independent_inodes_exact_bytes_and_existing_output_is_preserved(self):
        from mod_base.build_ci.validation import materialize_validation_export, verify_validation_export
        self.assertEqual(materialize_validation_export(self.root, self.output, **self.context), self.receipt)
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
        from mod_base.build_ci.validation import verify_validation_export
        data = b'{"observed":1,"observed":2}\n'
        report = self.receipt["reports"][0]
        (self.root / report["path"]).write_bytes(data)
        report.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.root / grammar.CI_VALIDATION_NAME).write_bytes(canonical_json(self.receipt))
        with self.assertRaises(MbError):
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
        from tests.test_ci_transport import target_set_fixture
        from mod_base.build_ci.transport import download_target_set
        plan, api, descriptors, partitions, *_ = target_set_fixture()
        with tempfile.TemporaryDirectory(prefix="mod-base-target-set-") as directory:
            output = Path(directory) / "inputs"
            self.assertEqual(download_target_set(api, descriptors=descriptors, plan=plan,
                workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2, output=output), partitions)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["target-0", "target-1"])
            for index, partition in enumerate(partitions):
                self.assertEqual(verify_build_export(output / f"target-{index}", plan=plan), partition["envelope"])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_real_late_target_zip_failure_leaves_no_partial_inputs_or_stage(self):
        if sys.platform != "linux":
            raise AssertionError("real target-set transport tests require Linux")
        from tests.test_ci_transport import target_set_fixture
        from mod_base.build_ci.transport import download_target_set
        plan, api, descriptors, *_ = target_set_fixture()
        selected = descriptors[1]["artifact"]
        data = b"not a ZIP"
        selected.update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
        api.add_artifact({"id": selected["id"], "name": selected["name"], "size_in_bytes": len(data),
                         "digest": selected["digest"], "created_at": selected["created_at"],
                         "expires_at": selected["expires_at"], "expired": False,
                         "workflow_run": {"id": 42, "head_branch": "master", "head_sha": "2" * 40}}, data)
        with tempfile.TemporaryDirectory(prefix="mod-base-target-set-") as directory:
            with self.assertRaises(MbError):
                download_target_set(api, descriptors=descriptors, plan=plan,
                    workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2,
                    output=Path(directory) / "inputs")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_real_same_attempt_target_download_before_aggregate_completion(self):
        if sys.platform != "linux":
            raise AssertionError("real target transport tests require Linux")
        from tests.test_ci_transport import transport_fixture
        from mod_base.build_ci.transport import download_target_partition
        plan, api, descriptor, envelope, *_ = transport_fixture(target=True)
        with tempfile.TemporaryDirectory(prefix="mod-base-target-download-") as directory:
            output = Path(directory) / "output"
            self.assertEqual(download_target_partition(api, descriptor=descriptor, plan=plan,
                workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2,
                target_id="target-a", output=output), envelope)
            self.assertEqual(verify_build_export(output, plan=plan), envelope)
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
            observed = download_completed_build(api, descriptor=descriptor, plan=plan,
                                                workflow_path=".github/workflows/build-gate.yml", output=output)
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
                download_completed_build(api, descriptor=descriptor, plan=plan,
                                         workflow_path=".github/workflows/build-gate.yml", output=output)
            self.assertEqual(list(Path(directory).iterdir()), [])


class LinuxBuildAssemblyTests(unittest.TestCase):
    def inputs(self, directory):
        if sys.platform != "linux":
            raise AssertionError("real complete Build assembly requires Linux")
        from tests.test_ci_transport import target_set_fixture
        from mod_base.build_ci.transport import download_target_set
        plan, api, descriptors, *_ = target_set_fixture()
        root = Path(directory) / "inputs"
        partitions = download_target_set(api, descriptors=descriptors, plan=plan,
            workflow_path=".github/workflows/build-gate.yml", run_id=42, run_attempt=2, output=root)
        return plan, partitions, root

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
        from tests.test_ci_gate_transport import gate_transport_fixture
        from mod_base.build_ci.transport import download_gate_receipt
        for kind in ("build", "packaged"):
            plan, api, document, descriptor, *_ = gate_transport_fixture(kind)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="mod-base-gate-download-") as directory:
                observed = download_gate_receipt(api, descriptor=descriptor, plan=plan, gate=kind,
                    workflow_path=descriptor["producer"]["workflow_path"],
                    build_workflow_path=".github/workflows/build-gate.yml", temporary_root=Path(directory))
                self.assertEqual(observed, document)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(api.mutations, [])

    def test_real_wrong_filename_and_noncanonical_record_leave_no_residue(self):
        if sys.platform != "linux":
            raise AssertionError("real tested-record transport requires Linux")
        from tests.test_ci_gate_transport import gate_transport_fixture
        from mod_base.build_ci.transport import download_gate_receipt
        for fixture in (gate_transport_fixture(filename="other.json"),
                        gate_transport_fixture(raw=canonical_json(gate_transport_fixture()[2]) + b"\n")):
            plan, api, _, descriptor, *_ = fixture
            with tempfile.TemporaryDirectory(prefix="mod-base-gate-reject-") as directory:
                with self.assertRaises(MbError):
                    download_gate_receipt(api, descriptor=descriptor, plan=plan, gate="build",
                        workflow_path=".github/workflows/build-gate.yml",
                        build_workflow_path=".github/workflows/build-gate.yml", temporary_root=Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])


class LinuxLatestBuildDownloadTests(unittest.TestCase):
    def test_real_newest_selection_download_and_atomic_copy(self):
        if sys.platform != "linux":
            raise AssertionError("real latest Build download requires Linux")
        from tests.test_ci_latest_download import latest_download_fixture
        from mod_base.build_ci.selection import download_latest_pr_build
        fixture = latest_download_fixture()
        with tempfile.TemporaryDirectory(prefix="mod-base-latest-build-") as directory:
            output = Path(directory) / "output"
            observed = download_latest_pr_build(fixture[1], plan=fixture[0],
                workflow_path=".github/workflows/build-gate.yml", output=output)
            self.assertEqual(observed, {"descriptor": fixture[2], "envelope": fixture[3]})
            for file in fixture[3]["files"]:
                self.assertEqual((output / file["path"]).read_bytes(), (file["path"] + "\n").encode())
            self.assertEqual(list(Path(directory).iterdir()), [output])
            self.assertEqual(fixture[1].mutations, [])

    def test_new_run_after_real_copy_prevents_publication_and_cleans_stage(self):
        if sys.platform != "linux":
            raise AssertionError("real latest Build race requires Linux")
        from tests.test_ci_latest_download import latest_download_fixture, supersede
        from mod_base.build_ci import exports
        from mod_base.build_ci.selection import download_latest_pr_build
        fixture = latest_download_fixture()
        original = exports.copy_regular_files
        def copying(*args, **kwargs):
            result = original(*args, **kwargs)
            supersede(fixture)
            return result
        with tempfile.TemporaryDirectory(prefix="mod-base-latest-race-") as directory, \
                patch.object(exports, "copy_regular_files", side_effect=copying):
            with self.assertRaises(MbError):
                download_latest_pr_build(fixture[1], plan=fixture[0],
                    workflow_path=".github/workflows/build-gate.yml", output=Path(directory) / "output")
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


class LinuxPythonInstallationCopyTests(unittest.TestCase):
    """Real root-owned temporary byte copying, not complete installed-runtime enrollment."""

    def run_copy_probe(self, failing: bool) -> bytes:
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("Python installation copying requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,io,tempfile,stat,gzip\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
            "from mod_base.build_ci import python_installation as install\n"
            "from mod_base.io.atomic_directory import atomic_directory\n"
            "from mod_base.errors import MbError\n"
            "import importlib.util\n"
            "spec=importlib.util.spec_from_file_location('copy_fixture',root/'tests/test_ci_python_installation.py')\n"
            # Load the fixture's imports explicitly from this trusted checkout too.
            "test_package=root/'tests'\n"
            "test_spec=importlib.util.spec_from_file_location('tests',test_package/'__init__.py',submodule_search_locations=[str(test_package)])\n"
            "test_module=importlib.util.module_from_spec(test_spec);sys.modules['tests']=test_module;test_spec.loader.exec_module(test_module)\n"
            "fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)\n"
            "assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            "compressed,digest,members=fixture.fixture();selected=install._selected(members,'3.11.17')\n"
            "with tempfile.TemporaryDirectory(prefix='mod-base-python-copy-') as directory:\n"
            " base=pathlib.Path(directory);output=base/'python'\n"
            f" failing={failing!r}\n"
            " data=gzip.compress(gzip.decompress(compressed).replace(b'known binary',b'changed data'),mtime=0) if failing else compressed\n"
            " def fill(path,fd): install._copy(io.BytesIO(data),fd,members,selected,len(data))\n"
            " if failing:\n"
            "  try: atomic_directory(output,fill)\n"
            "  except MbError: pass\n"
            "  else: raise AssertionError('changed archive copied')\n"
            "  assert list(base.iterdir())==[]\n"
            " else:\n"
            "  atomic_directory(output,fill)\n"
            "  assert (output/'bin/python3.11').read_bytes()==b'known binary'\n"
            "  assert (output/'empty.py').read_bytes()==b''\n"
            "  assert (output/'bin/python3').readlink()==pathlib.Path('python3.11')\n"
            "  assert stat.S_IMODE((output/'bin/python3.11').stat().st_mode)==0o755\n"
            "  assert stat.S_IMODE((output/'empty.py').stat().st_mode)==0o644\n"
            "  assert not (output/'setup.sh').exists() and not (output/'site.pth').exists() and not (output/'__pycache__').exists()\n"
            "  fd=os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
            "  try:\n"
            "   install._verify(fd,selected)\n"
            "   os.link(output/'empty.py',output/'hardlink')\n"
            "   try: install._verify(fd,selected)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('hardlinked installed file accepted')\n"
            "   (output/'hardlink').unlink()\n"
            "   (output/'empty.py').unlink();(output/'empty.py').symlink_to('bin/python3.11')\n"
            "   try: install._verify(fd,selected)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('substituted installed symlink accepted')\n"
            "  finally: os.close(fd)\n"
            "print('root-owned inert Python copy probe passed')\n"
        )
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        return result.stdout

    def test_real_root_owned_normalized_copy_and_tampering_rejection(self):
        self.assertEqual(self.run_copy_probe(False).strip(), b"root-owned inert Python copy probe passed")

    def test_real_failed_copy_cleans_the_unpublished_directory(self):
        self.assertEqual(self.run_copy_probe(True).strip(), b"root-owned inert Python copy probe passed")


class LinuxPythonArchiveCacheTests(unittest.TestCase):
    """Actual root-private temporary publication; no global cache or SDK mutation."""

    def run_cache_probe(self, failing: bool) -> bytes:
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("Python archive cache requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat,hashlib\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
            "from mod_base.build_ci import python_setup as setup\n"
            "from mod_base.io.atomic_directory import atomic_directory\n"
            "from mod_base.errors import MbError\n"
            "assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            "data=b'independently authored cache fixture';digest='sha256:'+hashlib.sha256(data).hexdigest()\n"
            "with tempfile.TemporaryDirectory(prefix='mod-base-python-cache-') as directory:\n"
            " base=pathlib.Path(directory);output=base/'cache'\n"
            f" failing={failing!r}\n"
            " def fill(path,fd):\n"
            "  setup._write(fd,data[:-1]+b'!' if failing else data)\n"
            "  setup._verify(fd,len(data),digest)\n"
            " if failing:\n"
            "  try: atomic_directory(output,fill)\n"
            "  except MbError: pass\n"
            "  else: raise AssertionError('altered archive published')\n"
            "  assert list(base.iterdir())==[]\n"
            " else:\n"
            "  atomic_directory(output,fill)\n"
            "  archive=output/'installer.tar.gz'\n"
            "  assert archive.read_bytes()==data\n"
            "  assert stat.S_IMODE(output.stat().st_mode)==0o700 and stat.S_IMODE(archive.stat().st_mode)==0o600\n"
            "  assert archive.stat().st_uid==archive.stat().st_gid==0\n"
            "  fd=os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
            "  try:\n"
            "   setup._verify(fd,len(data),digest)\n"
            "   os.link(archive,base/'hardlink')\n"
            "   try: setup._verify(fd,len(data),digest)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('hardlinked archive accepted')\n"
            "   (base/'hardlink').unlink()\n"
            "   archive.chmod(0o644)\n"
            "   try: setup._verify(fd,len(data),digest)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('public archive permissions accepted')\n"
            "   archive.chmod(0o600);archive.unlink();archive.symlink_to('../external')\n"
            "   try: setup._verify(fd,len(data),digest)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('symlink archive accepted')\n"
            "   archive.unlink();os.mkfifo(archive,0o600)\n"
            "   try: setup._verify(fd,len(data),digest)\n"
            "   except MbError: pass\n"
            "   else: raise AssertionError('FIFO archive accepted')\n"
            "  finally: os.close(fd)\n"
            "  try: atomic_directory(output,fill)\n"
            "  except MbError: pass\n"
            "  else: raise AssertionError('existing cache replaced')\n"
            "print('root-private inert Python archive cache probe passed')\n"
        )
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        return result.stdout

    def test_real_root_private_archive_copy_and_metadata_rejection(self):
        self.assertEqual(self.run_cache_probe(False).strip(), b"root-private inert Python archive cache probe passed")

    def test_changed_archive_cleans_unpublished_stage(self):
        self.assertEqual(self.run_cache_probe(True).strip(), b"root-private inert Python archive cache probe passed")


class LinuxPythonAuthenticationTests(unittest.TestCase):
    """Real temporary cache/SDK revalidation with explicit outer setup-location seams."""

    def test_source_derived_actual_sdk_admission_and_payload_mutation(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("Python authentication requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,io\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
            "test_package=root/'tests'\n"
            "spec=importlib.util.spec_from_file_location('tests',test_package/'__init__.py',submodule_search_locations=[str(test_package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['tests']=module;spec.loader.exec_module(module)\n"
            "from tests.test_ci_python_installation import fixture,BOUNDARY,KIT\n"
            "from mod_base.build_ci import python_setup as setup,python_installation as install\n"
            "from mod_base.io.atomic_directory import atomic_directory\n"
            "from mod_base.errors import MbError\n"
            "from unittest.mock import patch\n"
            "assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            "data,digest,members=fixture();selected=install._selected(members,'3.11.17')\n"
            "with tempfile.TemporaryDirectory(prefix='mod-base-python-auth-') as directory:\n"
            " base=pathlib.Path(directory);cache_parent=base/'cache';prefix=base/'sdk'\n"
            " cache_parent.mkdir(mode=0o700);prefix.mkdir(mode=0o755)\n"
            " def fill_cache(path,fd): setup._write(fd,data);setup._verify(fd,len(data),digest)\n"
            " atomic_directory(cache_parent/'3.11.17',fill_cache)\n"
            " def fill_sdk(path,fd): install._copy(io.BytesIO(data),fd,members,selected,len(data))\n"
            " atomic_directory(prefix/'x64',fill_sdk)\n"
            " original=(prefix/'x64').stat()\n"
            " proof=install.PythonInstallation('3.11.17',digest,install._manifest(selected,'3.11.17',digest),len(selected),\n"
            "  sum(item.kind=='file' for item in selected),sum(item.size for item in selected),original.st_dev,original.st_ino)\n"
            " def parent(path): return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
            " def role(boundary): assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            " with patch.object(setup,'authenticate_privileged_host_boundary',role),patch.object(setup,'_admit_lock'),\\\n"
            "      patch.object(setup,'_PROFILES',{'3.11.17':(len(data),digest[7:])}),\\\n"
            "      patch.object(setup,'_parent',side_effect=lambda **kwargs:parent(cache_parent)),\\\n"
            "      patch.object(setup,'_installation_parent',side_effect=lambda *args,**kwargs:parent(prefix)):\n"
            "  assert setup.authenticate_privileged_python_installation(proof,boundary=BOUNDARY,installation=KIT)=='/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11'\n"
            "  (prefix/'x64/bin/python3.11').write_bytes(b'changed data')\n"
            "  try: setup.authenticate_privileged_python_installation(proof,boundary=BOUNDARY,installation=KIT)\n"
            "  except MbError: pass\n"
            "  else: raise AssertionError('mutated SDK authenticated')\n"
            "print('source-derived actual Python SDK authentication probe passed')\n"
        )
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"source-derived actual Python SDK authentication probe passed")


class LinuxGradleCacheCopyTests(unittest.TestCase):
    """Real temporary independent cache copies, with explicit outer account/location seams."""

    def test_private_seed_copy_and_configuration_rejection(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("Gradle seed copying requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
            "from mod_base.build_ci import gradle_cache as cache\n"
            "from mod_base.build_ci.worker import WorkerAccount\n"
            "from mod_base.io.tree import regular_data_records\n"
            "from mod_base.errors import MbError\n"
            "from unittest.mock import patch\n"
            "from types import SimpleNamespace\n"
            "assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            "with tempfile.TemporaryDirectory(prefix='mod-base-gradle-seed-') as directory:\n"
            " base=pathlib.Path(directory);seed=base/'seed';seed.mkdir(mode=0o700)\n"
            " (seed/'caches').mkdir();(seed/'wrapper').mkdir()\n"
            " (seed/'caches/module.jar').write_bytes(b'known cache bytes');(seed/'wrapper/empty').write_bytes(b'')\n"
            " original=regular_data_records(seed,**cache._BOUNDS)\n"
            " worker_root=base/'worker';home=worker_root/'worker-home';home.mkdir(parents=True)\n"
            " target=home/'gradle-home';target.mkdir(mode=0o700);os.chown(target,2001,2001)\n"
            " account=WorkerAccount('worker',2001,2001,str(home));boundary=SimpleNamespace(home=str(base),uid=1001,gid=121)\n"
            " def role(receipt): assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0\n"
            " with patch.object(cache,'WORKER_ROOT',pathlib.PurePosixPath(worker_root)),patch.object(cache,'authenticate_privileged_host_boundary',role),\\\n"
            "      patch.object(cache,'authenticate_worker_account',return_value=account),patch.object(cache,'_quiet'),patch.object(cache,'terminate_worker') as terminate:\n"
            "  assert cache.stage_privileged_gradle_cache(seed,boundary=boundary,account=account)==original\n"
            "  assert (target/'caches/module.jar').read_bytes()==b'known cache bytes' and (target/'wrapper/empty').read_bytes()==b''\n"
            "  assert (target/'caches/module.jar').stat().st_ino!=(seed/'caches/module.jar').stat().st_ino\n"
            "  assert target.stat().st_uid==target.stat().st_gid==2001 and stat.S_IMODE(target.stat().st_mode)==0o700\n"
            "  assert stat.S_IMODE((target/'caches/module.jar').stat().st_mode)==0o600\n"
            "  terminate.assert_not_called()\n"
            "  (seed/'gradle.properties').write_bytes(b'credential fixture')\n"
            "  try: cache.stage_privileged_gradle_cache(seed,boundary=boundary,account=account)\n"
            "  except MbError: pass\n"
            "  else: raise AssertionError('configuration seed accepted')\n"
            "  terminate.assert_called_once_with(account)\n"
            "  assert (target/'caches/module.jar').read_bytes()==b'known cache bytes'\n"
            "print('private independent Gradle cache copy probe passed')\n"
        )
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"private independent Gradle cache copy probe passed")


class LinuxWorkerOverlayCopyTests(unittest.TestCase):
    """Real temporary overlay publication; outer provenance/account seams remain explicit."""

    def test_empty_sources_roots_and_plain_candidate_ownership(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("worker overlay copying requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat,hashlib\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
        ) + r"""
from mod_base.build_ci import worker_overlay as overlay
from mod_base.build_ci.worker import WorkerAccount
from mod_base.pin import Pin, stamp_document, STAMP_NAME
from mod_base.model.canonical import canonical_json
from mod_base.errors import MbError
from unittest.mock import patch
from types import SimpleNamespace
assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
# Expectations derive from authored fixture bytes, before any unknown copied tree is inspected.
fixture={'src/mod_base/__init__.py':b'', 'src/mod_base/template/staged_files.sha256':b''}
listing=''.join(hashlib.sha256(data).hexdigest()+'  ./'+path+'\n' for path,data in sorted(fixture.items()))
digest='sha256:'+hashlib.sha256(listing.encode('ascii')).hexdigest()
pin=Pin('a'*40,'v1.0.3',())
with tempfile.TemporaryDirectory(prefix='mod-base-worker-overlay-') as directory:
 base=pathlib.Path(directory);seed=base/'seed';seed.mkdir(mode=0o700)
 for top in ('src','site','requirements'): (seed/top).mkdir(mode=0o755)
 for path,data in fixture.items():
  leaf=seed/path;leaf.parent.mkdir(parents=True,exist_ok=True);leaf.write_bytes(data);leaf.chmod(0o644)
 (seed/STAMP_NAME).write_bytes(canonical_json(stamp_document(pin,digest)))
 worker_root=base/'worker';repository=worker_root/'repository';repository.mkdir(parents=True)
 account=WorkerAccount('worker',2001,2001,str(worker_root/'worker-home'))
 boundary=SimpleNamespace(home=str(base),uid=1001,gid=121)
 def role(receipt): assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
 with patch.object(overlay,'WORKER_ROOT',pathlib.PurePosixPath(worker_root)),patch.object(overlay,'authenticate_privileged_host_boundary',role),patch.object(overlay,'authenticate_worker_account',return_value=account),patch.object(overlay,'_quiet'),patch.object(overlay,'verify_released') as release,patch.object(overlay,'terminate_worker') as terminate:
  observed=overlay.stage_privileged_worker_overlay(object(),seed,boundary=boundary,account=account,pin=pin,expected_digest=digest)
  target=repository/'out/mod-base-kit'
  assert len(observed)==3 and release.call_count==3
  for path,data in fixture.items():
   leaf=target/path;assert leaf.read_bytes()==data and leaf.stat().st_ino!=(seed/path).stat().st_ino
   assert leaf.stat().st_uid==leaf.stat().st_gid==2001 and stat.S_IMODE(leaf.stat().st_mode)==0o644
  for top in ('src','site','requirements'):
   info=(target/top).stat();assert info.st_uid==info.st_gid==2001 and stat.S_IMODE(info.st_mode)==0o755
  terminate.assert_not_called()
  try: overlay.stage_privileged_worker_overlay(object(),seed,boundary=boundary,account=account,pin=pin,expected_digest=digest)
  except MbError: pass
  else: raise AssertionError('existing candidate overlay overwritten')
  terminate.assert_called_once_with(account)
  assert (target/'src/mod_base/__init__.py').read_bytes()==b''
print('independent plain worker overlay copy probe passed')
"""
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"independent plain worker overlay copy probe passed")


class LinuxWorkerSourceCopyTests(unittest.TestCase):
    """Real temporary tracked-source handoff with explicit outer role/account/location seams."""

    def test_private_source_preserves_git_modes_and_never_chowns_link_target(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("worker source copying requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat,hashlib\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
        ) + r"""
from mod_base.build_ci import worker_source as source
from mod_base.build_ci.source import GitSourceEntry
from mod_base.build_ci.worker import WorkerAccount
from mod_base.errors import MbError
from unittest.mock import patch
from types import SimpleNamespace
assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
with tempfile.TemporaryDirectory(prefix='mod-base-worker-source-') as directory:
 base=pathlib.Path(directory);seed=base/'seed';seed.mkdir(mode=0o700)
 outside=base/'outside';outside.write_bytes(b'protected sentinel');outside.chmod(0o600)
 target_bytes=os.fsencode(outside)
 fixture={'dir/empty':('100644',b''),'link':('120000',target_bytes),'script.sh':('100755',b'known script bytes')}
 # Git expectations are authored before any unknown source/copy hashes are read.
 inventory=tuple(GitSourceEntry(path,mode,len(data),hashlib.sha1(b'blob '+str(len(data)).encode('ascii')+b'\0'+data).hexdigest()) for path,(mode,data) in sorted(fixture.items()))
 for path,(mode,data) in fixture.items():
  leaf=seed/path;leaf.parent.mkdir(parents=True,exist_ok=True)
  if mode=='120000': os.symlink(data,leaf)
  else: leaf.write_bytes(data);leaf.chmod(0o755 if mode=='100755' else 0o644)
 (seed/'.git').mkdir();(seed/'.git/config').write_bytes(b'opaque metadata must not be copied')
 worker_root=base/'worker';worker_root.mkdir()
 account=WorkerAccount('worker',2001,2001,str(worker_root/'worker-home'))
 boundary=SimpleNamespace(home=str(base),uid=1001,gid=121)
 before=outside.stat()
 def role(receipt): assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
 with patch.object(source,'WORKER_ROOT',pathlib.PurePosixPath(worker_root)),patch.object(source,'authenticate_privileged_host_boundary',role),patch.object(source,'authenticate_worker_account',return_value=account),patch.object(source,'_quiet'),patch.object(source,'terminate_worker') as terminate:
  observed=source.stage_privileged_worker_source(seed,boundary=boundary,account=account,inventory=inventory)
  repository=worker_root/'repository'
  assert len(observed)==3 and not (repository/'.git').exists()
  assert repository.stat().st_uid==repository.stat().st_gid==2001 and stat.S_IMODE(repository.stat().st_mode)==0o700
  for path,(mode,data) in fixture.items():
   leaf=repository/path;info=leaf.lstat()
   assert info.st_uid==info.st_gid==2001 and info.st_ino!=(seed/path).lstat().st_ino
   if mode=='120000': assert os.readlink(os.fsencode(leaf))==data
   else: assert leaf.read_bytes()==data and stat.S_IMODE(info.st_mode)==(0o700 if mode=='100755' else 0o600)
  after=outside.stat();assert (after.st_dev,after.st_ino,after.st_uid,after.st_gid,after.st_mode,after.st_ctime_ns)==(before.st_dev,before.st_ino,before.st_uid,before.st_gid,before.st_mode,before.st_ctime_ns)
  assert outside.read_bytes()==b'protected sentinel'
  terminate.assert_not_called()
  try: source.stage_privileged_worker_source(seed,boundary=boundary,account=account,inventory=inventory)
  except MbError: pass
  else: raise AssertionError('existing worker repository overwritten')
  terminate.assert_called_once_with(account)
print('independent private worker source copy probe passed')
"""
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"independent private worker source copy probe passed")


class LinuxWorkerGitCopyTests(unittest.TestCase):
    """Real temporary Git-data curation with explicit outer checkout/role/account seams."""

    def test_private_metadata_has_known_head_config_objects_and_no_source_tokens_or_hooks(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("worker Git copying requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat,hashlib\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
        ) + r"""
from mod_base.build_ci import worker_git as git
from mod_base.build_ci.worker import WorkerAccount
from mod_base.errors import MbError
from unittest.mock import patch
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('authored_git_fixture',root/'tests/test_ci_git_fixture.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
commit,tree,known=fixture.git_fixture()
assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
with tempfile.TemporaryDirectory(prefix='mod-base-worker-git-') as directory:
 base=pathlib.Path(directory);seed=base/'source-git';seed.mkdir(mode=0o700)
 for name,data in {**known,'HEAD':(commit+'\n').encode(),'config':b'[http]\n extraheader = fixture secret\n'}.items():
  leaf=seed/name;leaf.parent.mkdir(parents=True,exist_ok=True);leaf.write_bytes(data);leaf.chmod(0o644)
 (seed/'hooks').mkdir();(seed/'hooks/post-checkout').write_bytes(b'fixture hook must not execute');(seed/'hooks/post-checkout').chmod(0o755)
 worker_root=base/'worker';repository=worker_root/'repository';repository.mkdir(mode=0o700,parents=True);os.chown(repository,2001,2001)
 account=WorkerAccount('worker',2001,2001,str(worker_root/'worker-home'))
 boundary=SimpleNamespace(home=str(base),uid=1001,gid=121)
 def role(receipt):assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
 with patch.object(git,'WORKER_ROOT',pathlib.PurePosixPath(worker_root)),patch.object(git,'authenticate_privileged_host_boundary',role),patch.object(git,'authenticate_worker_account',return_value=account),patch.object(git,'_quiet'),patch.object(git,'terminate_worker') as terminate:
  observed=git.stage_privileged_worker_git(seed,boundary=boundary,account=account,repository='owner/project',tested_commit=commit)
  output=repository/'.git'
  assert output.stat().st_uid==output.stat().st_gid==2001 and stat.S_IMODE(output.stat().st_mode)==0o700
  assert (output/'HEAD').read_bytes()==(commit+'\n').encode() and (output/'config').read_bytes()==git._configuration('owner/project')
  assert not (output/'hooks').exists() and b'fixture secret' not in (output/'config').read_bytes()
  for name,data in known.items():
   leaf=output/name;info=leaf.stat()
   assert leaf.read_bytes()==data and info.st_ino!=(seed/name).stat().st_ino
   assert info.st_uid==info.st_gid==2001 and stat.S_IMODE(info.st_mode)==0o600
  assert len(observed)==len(known)+2
  terminate.assert_not_called()
  try:git.stage_privileged_worker_git(seed,boundary=boundary,account=account,repository='owner/project',tested_commit=commit)
  except MbError:pass
  else:raise AssertionError('existing Git metadata overwritten')
  terminate.assert_called_once_with(account)
print('independent private worker Git data copy probe passed')
"""
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"independent private worker Git data copy probe passed")


class LinuxWorkerPreparationTests(unittest.TestCase):
    """Real temporary composed copying; outer API/checkout/host/account/runtime seams remain."""

    def test_composed_source_git_cache_overlay_and_reserved_root_binding(self):
        if sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise AssertionError("composed worker preparation requires the hosted Linux fixture")
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.util,sys,pathlib,os,tempfile,stat,hashlib\n"
            f"root=pathlib.Path({str(root)!r})\n"
            "package=root/'src/mod_base'\n"
            "spec=importlib.util.spec_from_file_location('mod_base',package/'__init__.py',submodule_search_locations=[str(package)])\n"
            "module=importlib.util.module_from_spec(spec);sys.modules['mod_base']=module;spec.loader.exec_module(module)\n"
        ) + r"""
from mod_base.build_ci import worker_preparation as prep,worker_source,worker_git,gradle_cache,worker_overlay
from mod_base.build_ci.source import GitSourceEntry
from mod_base.build_ci.worker import WorkerAccount
from mod_base.model.canonical import canonical_json
from mod_base.pin import Pin,stamp_document,STAMP_NAME
from mod_base.errors import MbError
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('authored_git_fixture',root/'tests/test_ci_git_fixture.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
commit,tree,git_files=fixture.git_fixture()
file_data=b'known tracked bytes\n'
blob=hashlib.sha1(b'blob '+str(len(file_data)).encode()+b'\0'+file_data).hexdigest()
inventory=(GitSourceEntry('file','100644',len(file_data),blob),)
kit_files={'src/mod_base/__init__.py':b'','src/mod_base/template/staged_files.sha256':b''}
listing=''.join(hashlib.sha256(data).hexdigest()+'  ./'+name+'\n' for name,data in sorted(kit_files.items()))
digest='sha256:'+hashlib.sha256(listing.encode('ascii')).hexdigest();pin=Pin('a'*40,'v1.0.3',())
identity={'repository':'owner/project','tested_sha':commit,'tested_tree':tree}
assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
with tempfile.TemporaryDirectory(prefix='mod-base-worker-preparation-') as directory:
 base=pathlib.Path(directory);source=base/'source';source.mkdir(mode=0o700);(source/'file').write_bytes(file_data)
 git_source=source/'.git';git_source.mkdir(mode=0o700)
 for name,data in {**git_files,'HEAD':(commit+'\n').encode(),'config':b'[http]\n extraheader = fixture secret\n'}.items():
  leaf=git_source/name;leaf.parent.mkdir(parents=True,exist_ok=True);leaf.write_bytes(data);leaf.chmod(0o644)
 seed=base/'gradle-seed';seed.mkdir(mode=0o700);(seed/'caches').mkdir();(seed/'wrapper').mkdir()
 (seed/'caches/module.jar').write_bytes(b'known Gradle cache');(seed/'wrapper/empty').write_bytes(b'')
 overlay=base/'overlay';overlay.mkdir(mode=0o700)
 for top in ('src','site','requirements'):(overlay/top).mkdir()
 for name,data in kit_files.items():
  leaf=overlay/name;leaf.parent.mkdir(parents=True,exist_ok=True);leaf.write_bytes(data);leaf.chmod(0o644)
 (overlay/STAMP_NAME).write_bytes(canonical_json(stamp_document(pin,digest)))
 worker_root=base/'worker';home=worker_root/'worker-home';home.mkdir(parents=True,mode=0o700);os.chown(home,2001,2001)
 cache=home/'gradle-home';cache.mkdir(mode=0o700);os.chown(cache,2001,2001);cache_id=(cache.stat().st_dev,cache.stat().st_ino)
 account=WorkerAccount('worker',2001,2001,str(home));boundary=SimpleNamespace(home=str(base),uid=1001,gid=121)
 def role(receipt):assert os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0
 with ExitStack() as stack:
  for component in (prep,worker_source,worker_git,gradle_cache,worker_overlay):
   stack.enter_context(patch.object(component,'WORKER_ROOT',pathlib.PurePosixPath(worker_root)))
   stack.enter_context(patch.object(component,'authenticate_privileged_host_boundary',role))
   stack.enter_context(patch.object(component,'authenticate_worker_account',return_value=account))
   stack.enter_context(patch.object(component,'_quiet'))
   stack.enter_context(patch.object(component,'terminate_worker'))
  stack.enter_context(patch.object(prep,'authenticate_source_inventory',return_value=inventory))
  final_api=stack.enter_context(patch.object(prep,'authenticate_source_identity'))
  stack.enter_context(patch.object(prep,'verify_released'));stack.enter_context(patch.object(worker_overlay,'verify_released'))
  observed=prep.prepare_privileged_worker_checkout(object(),source,seed,overlay,boundary=boundary,account=account,identity=identity,pin=pin,expected_digest=digest)
  repository=worker_root/'repository'
  assert set(observed)=={'source','git','gradle','overlay'} and (repository/'file').read_bytes()==file_data
  assert (repository/'.git/HEAD').read_bytes()==(commit+'\n').encode() and b'fixture secret' not in (repository/'.git/config').read_bytes()
  assert (cache/'caches/module.jar').read_bytes()==b'known Gradle cache' and (cache/'wrapper/empty').read_bytes()==b''
  assert (cache.stat().st_dev,cache.stat().st_ino)==cache_id
  assert (repository/'out/mod-base-kit/site').is_dir() and (repository/'out/mod-base-kit/requirements').is_dir()
  assert (repository/'out/mod-base-kit/src/mod_base/__init__.py').read_bytes()==b''
  for private in (repository,repository/'.git',cache):
   info=private.stat();assert info.st_uid==info.st_gid==2001 and stat.S_IMODE(info.st_mode)==0o700
  assert (repository/'file').stat().st_ino!=(source/'file').stat().st_ino
  final_api.assert_called_once()
print('composed private worker checkout preparation probe passed')
"""
        result = command("/usr/bin/sudo", "-n", "--", sys.executable, "-I", "-B", "-S", "-c", program, cwd=root)
        self.assertEqual(result.stdout.strip(), b"composed private worker checkout preparation probe passed")


if __name__ == "__main__":
    unittest.main()
