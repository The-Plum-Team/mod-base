"""Independent stdlib guard semantics; Windows seams do not prove the Linux boundary."""

import copy
import errno
import hashlib
import importlib.util
import stat
import subprocess
import sys
import tempfile
import unittest
import io
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import DIGESTED_DIRS
from tests.helpers import ci_kit_installation


PROGRAM = Path(__file__).resolve().parents[1] / "tools/ci_privileged_bootstrap.py"
spec = importlib.util.spec_from_file_location("ci_privileged_bootstrap_tests", PROGRAM)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
EXPECTED = dict(expected_sha="a" * 40, expected_version="1.0.3", expected_digest="sha256:" + "b" * 64)


def entry_arguments():
    values = ["a"*40,"1.0.3","sha256:"+"b"*64,"Owner/Mod","c"*40,"/home/runner/controller",
              "1001","121","0","10","493","d"*64]
    return [item for pair in zip(bootstrap.ENTRY_FLAGS,values) for item in pair]


class PrivilegedBootstrapTests(unittest.TestCase):
    def test_closed_entry_rejects_unknown_duplicate_unbounded_and_inexact_arguments_before_loading(self):
        valid = entry_arguments()
        self.assertEqual(bootstrap._entry_arguments(valid)["--nonce"],"d"*64)
        cases = [[],valid[:-1],valid+["--command","echo forged"],tuple(valid)]
        for index,value in ((0,"--command"),(2,"--kit-sha"),(13,"01"),(15,"0"),
                (17,str(1<<64)),(19,"0"),(21,str(0o10000)),(23,"D"*64),
                (11,"x"*4097),(11,"\ud800"),(11,"/home/runner/\ncontroller"),(13,True)):
            changed=valid.copy();changed[index]=value;cases.append(changed)
        for arguments in cases:
            with self.subTest(arguments=arguments),patch.object(bootstrap,"load_fixed_kit") as loading, \
                    redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(bootstrap.main(arguments),2)
            loading.assert_not_called()
            self.assertEqual(stderr.getvalue(),"mod-base: private bootstrap rejected\n")

    def test_actual_process_entry_fails_closed_without_loading_kit_on_unsupported_host(self):
        # Wrong process arguments fail on every host. Correct arguments on Windows then fail
        # the Linux/root byte guard; no physical admission seam is used in these subprocesses.
        cases=[[]]
        if sys.platform!="linux":cases.append(entry_arguments())
        for arguments in cases:
            result=subprocess.run((sys.executable,"-I","-B","-S",str(PROGRAM),*arguments),
                stdin=subprocess.DEVNULL,capture_output=True,timeout=20,check=False)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,b"")
            self.assertEqual(result.stderr.replace(b"\r\n",b"\n"),b"mod-base: private bootstrap rejected\n")

    def test_entry_pre_dispatch_failures_never_report_success_or_echo_payloads(self):
        for error,code in ((bootstrap.BootstrapError("\n::error::payload"),2),
                (RuntimeError("\n::error::payload"),1),(SystemExit(0),1),(KeyboardInterrupt(),130)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(bootstrap,"load_fixed_kit",side_effect=error),redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(bootstrap.main(entry_arguments()),code)
            self.assertEqual(len(stderr.getvalue().splitlines()),1)
            self.assertNotIn("payload",stderr.getvalue())

    def test_entry_loads_first_composes_explicit_context_and_calls_only_fixed_sealing(self):
        from mod_base.build_ci import host,worker,root_request
        from mod_base.errors import MbError
        from tests.test_ci_root_request import fixture,VALIDATOR
        invocation,*_=fixture()
        for failure in (None,"composition","sealing"):
            events=[]
            with self.subTest(failure=failure),ExitStack() as stack:
                loading=stack.enter_context(patch.object(bootstrap,"load_fixed_kit",side_effect=lambda **kw:events.append("load")))
                stack.enter_context(patch.object(host,"authenticate_privileged_host_boundary",side_effect=lambda boundary:events.append("host")))
                account=stack.enter_context(patch.object(worker,"authenticate_worker_account",return_value=VALIDATOR))
                def compose(**kwargs):
                    events.append("compose")
                    if failure=="composition":raise MbError("fixture composition")
                    return invocation
                composition=stack.enter_context(patch.object(root_request,"build_root_freeze_invocation",side_effect=compose))
                sealing=stack.enter_context(patch.object(root_request,"freeze_root_requested_build_validation",
                    side_effect=MbError("fixture sealing") if failure=="sealing" else None))
                stopping=stack.enter_context(patch.object(worker,"terminate_worker"))
                stack.enter_context(redirect_stderr(io.StringIO()))
                self.assertEqual(bootstrap.main(entry_arguments()),0 if failure is None else 2)
                self.assertEqual(events,["load","host","compose"])
                loading.assert_called_once_with(**EXPECTED)
                account.assert_called_once_with("validator")
                args=composition.call_args.kwargs
                self.assertEqual(args["controller_root"],"/home/runner/controller")
                self.assertEqual(args["repository"],"Owner/Mod")
                self.assertEqual(args["controller_sha"],"c"*40)
                self.assertEqual(args["kit_sha"],"a"*40)
                if failure=="composition":
                    sealing.assert_not_called();stopping.assert_called_once_with(VALIDATOR)
                else:
                    sealing.assert_called_once_with(invocation,boundary=args["boundary"],validator=VALIDATOR,nonce="d"*64)
                    stopping.assert_not_called() # The real fixed sealing API owns termination.

    def test_actual_isolated_fixed_package_load_and_failure_cleanup(self):
        # Only byte admission is an explicit seam; actual importlib package/submodule loading
        # runs under -I/-B/-S against fresh fixture source, with a rejecting cwd shadow.
        for mode in ("good", "pre-admission", "post-admission", "wrong-version", "import-error"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "src/mod_base"
                package.mkdir(parents=True)
                source = ("__version__='1.0.3'\nKIT_REPOSITORY='The-Plum-Team/mod-base'\n"
                          "from mod_base import sibling\n")
                if mode == "import-error":
                    source += "raise RuntimeError('fixture initialization failure')\n"
                (package / "__init__.py").write_text(source, encoding="utf-8")
                (package / "sibling.py").write_text("VALUE=17\n", encoding="utf-8")
                (root / "mod_base.py").write_text("raise RuntimeError('cwd shadow imported')\n", encoding="utf-8")
                body = ("import importlib.util,sys\n"
                        f"spec=importlib.util.spec_from_file_location('guard',{str(PROGRAM)!r})\n"
                        "guard=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(guard)\n"
                        f"guard.KIT_ROOT={str(root)!r}\nmode={mode!r}\n"
                        "original=tuple(sys.path)\ncalls=[]\n"
                        "def admitted(**kwargs):\n"
                        "    calls.append(kwargs)\n"
                        "    if mode=='pre-admission' or mode=='post-admission' and len(calls)==2:\n"
                        "        raise guard.BootstrapError('fixture admission failure')\n"
                        "guard.authenticate_fixed_kit=admitted\n"
                        "expected=dict(expected_sha='a'*40,expected_version='2.0.0' if mode=='wrong-version' else '1.0.3',"
                        "expected_digest='sha256:'+'b'*64)\n"
                        "try:\n    module=guard.load_fixed_kit(**expected)\n"
                        "except guard.BootstrapError:\n"
                        "    assert mode!='good'\n"
                        "    assert not any(n=='mod_base' or n.startswith('mod_base.') for n in sys.modules)\n"
                        "else:\n"
                        "    assert mode=='good' and module.sibling.VALUE==17 and calls==[expected,expected]\n"
                        "    assert sys.modules['mod_base'] is module\n"
                        "    try:\n        guard.load_fixed_kit(**expected)\n"
                        "    except guard.BootstrapError:\n        pass\n"
                        "    else:\n        raise AssertionError('preloaded kit admitted')\n"
                        "    assert len(calls)==2 and sys.modules['mod_base'] is module\n"
                        "assert tuple(sys.path)==original\n")
                result = subprocess.run((sys.executable,"-I","-B","-S","-c",body),cwd=root,
                                        stdin=subprocess.DEVNULL,capture_output=True,timeout=20,check=False)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertFalse(any(root.rglob("__pycache__")))

    def test_independent_contract_constants_match_central_definitions(self):
        self.assertEqual(bootstrap.ROOTS, DIGESTED_DIRS)
        self.assertEqual(bootstrap.RECORD_NAME, grammar.CI_KIT_INSTALLATION_NAME)
        for local, central in (("MAX_RECORD_BYTES", "MAX_CI_KIT_INSTALL_RECORD_BYTES"),
                               ("MAX_FILES", "MAX_CI_KIT_INSTALL_FILES"),
                               ("MAX_ENTRIES", "MAX_CI_KIT_INSTALL_ENTRIES"),
                               ("MAX_BYTES", "MAX_CI_KIT_INSTALL_BYTES"),
                               ("MAX_DEPTH", "MAX_CI_TOOL_TREE_DEPTH"),
                               ("MAX_FILE_ID", "MAX_CI_FILE_ID"),
                               ("MAX_UNIX_ID", "MAX_CI_UNIX_ID"),
                               ("MAX_ARGUMENT_BYTES", "MAX_CI_TOOL_PATH_BYTES"),
                               ("READ_BYTES", "CI_PROCESS_READ_BYTES")):
            self.assertEqual(getattr(bootstrap, local), getattr(limits, central))
        for version in ("0.0.0", "1.0.3", "999999.999999.999999", "01.0.0", "1.0.0\n", "v1.0.0"):
            self.assertEqual(bool(bootstrap.VERSION.fullmatch(version)), bool(grammar.VERSION.fullmatch(version)))

    def test_record_binds_explicit_approved_identity_and_closed_canonical_bytes(self):
        document = ci_kit_installation()
        args = (EXPECTED["expected_sha"], EXPECTED["expected_version"], EXPECTED["expected_digest"])
        self.assertEqual(bootstrap._record(canonical_json(document), *args), document)
        for mutate in (lambda d: d.update(program="candidate"), lambda d: d.update(files=True),
                       lambda d: d.update(device=-1), lambda d: d.update(inode=0),
                       lambda d: d.update(inode=bootstrap.MAX_FILE_ID+1),
                       lambda d: d.update(schema_version=True), lambda d: d.update(schema_version=2),
                       lambda d: d["kit"].update(sha="c" * 40), lambda d: d.update(tree_digest="sha256:"+"c"*64)):
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(bootstrap.BootstrapError):
                bootstrap._record(canonical_json(changed), *args)
        for raw in (canonical_json(document)+b" ", b'{"kind":1,"kind":2}', b'{"value":NaN}',
                    b" "*(bootstrap.MAX_RECORD_BYTES+1)):
            with self.subTest(raw=raw[:40]), self.assertRaises(bootstrap.BootstrapError):
                bootstrap._record(raw, *args)

    def test_isolated_program_load_imports_no_kit_and_changes_no_search_path(self):
        body = ("import importlib.util,sys\noriginal=tuple(sys.path)\n"
                f"spec=importlib.util.spec_from_file_location('guard',{str(PROGRAM)!r})\n"
                "module=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                "assert tuple(sys.path)==original\n"
                "assert not any(n=='mod_base' or n.startswith('mod_base.') for n in sys.modules)\n")
        result = subprocess.run((sys.executable, "-I", "-B", "-S", "-c", body),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_role_and_isolation_rejection_precede_filesystem_access(self):
        with patch.object(bootstrap.sys, "platform", "win32"), patch.object(bootstrap, "_open_directory") as opening:
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.authenticate_fixed_kit(**EXPECTED)
            opening.assert_not_called()
        with patch.object(bootstrap.sys, "platform", "linux"), patch.object(bootstrap.os, "getuid", return_value=1001, create=True), \
                patch.object(bootstrap, "_open_directory") as opening:
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.authenticate_fixed_kit(**EXPECTED)
            opening.assert_not_called()

    def test_private_metadata_rejects_owner_mode_hardlink_and_acl(self):
        info = SimpleNamespace(st_dev=1, st_ino=11, st_mode=stat.S_IFREG | 0o600,
                               st_uid=0, st_gid=0, st_nlink=1, st_size=3, st_mtime_ns=1, st_ctime_ns=1)
        def no_acl(*args):
            raise OSError(errno.ENODATA, "absent ACL fixture")
        with patch.object(bootstrap.os, "fstat", return_value=info), \
                patch.object(bootstrap.os, "getxattr", side_effect=no_acl, create=True):
            self.assertEqual(bootstrap._private(7, directory=False), bootstrap._stamp(info))
            for field, value in (("st_uid", 1001), ("st_gid", 121), ("st_nlink", 2),
                                 ("st_mode", stat.S_IFREG | 0o644), ("st_mode", stat.S_IFIFO | 0o600)):
                previous = getattr(info, field)
                setattr(info, field, value)
                with self.subTest(field=field), self.assertRaises(bootstrap.BootstrapError):
                    bootstrap._private(7, directory=False)
                setattr(info, field, previous)
        with patch.object(bootstrap.os, "fstat", return_value=info), \
                patch.object(bootstrap.os, "getxattr", return_value=b"ACL fixture", create=True), \
                self.assertRaises(bootstrap.BootstrapError):
            bootstrap._private(7, directory=False)

    def _root_seams(self, stack):
        stack.enter_context(patch.object(bootstrap.sys, "platform", "linux"))
        stack.enter_context(patch.object(bootstrap.sys, "flags", SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1)))
        for name in ("getuid", "geteuid", "getgid", "getegid"):
            stack.enter_context(patch.object(bootstrap.os, name, return_value=0, create=True))

    def test_fixed_admission_brackets_record_inventory_named_root_and_closes_descriptors(self):
        raw = canonical_json(ci_kit_installation())
        with ExitStack() as stack:
            self._root_seams(stack)
            opening = stack.enter_context(patch.object(bootstrap, "_open_directory", side_effect=[10, 11, 12, 13]))
            stack.enter_context(patch.object(bootstrap, "_private", return_value=(1, 11)))
            stack.enter_context(patch.object(bootstrap, "_names", return_value=[bootstrap.RECORD_NAME]))
            stack.enter_context(patch.object(bootstrap, "_file", return_value=(raw, len(raw))))
            inventory = stack.enter_context(patch.object(bootstrap, "_inventory", return_value=(EXPECTED["expected_digest"], 3, 3)))
            closing = stack.enter_context(patch.object(bootstrap.os, "close"))
            bootstrap.authenticate_fixed_kit(**EXPECTED)
            self.assertEqual([call.args[0] for call in opening.call_args_list],
                             [bootstrap.RECORD_ROOT, bootstrap.KIT_ROOT, bootstrap.RECORD_ROOT, bootstrap.KIT_ROOT])
            inventory.assert_called_once_with(11)
            self.assertEqual([call.args[0] for call in closing.call_args_list], [12, 13, 11, 10])
        for failure in ("second-open", "inventory", "record-drift", "named-root"):
            with self.subTest(failure=failure), ExitStack() as stack:
                self._root_seams(stack)
                stack.enter_context(patch.object(bootstrap, "_open_directory", side_effect=
                    [10, OSError("fixture")] if failure == "second-open" else [10, 11, 12, 13]))
                stack.enter_context(patch.object(bootstrap, "_private", side_effect=
                    [(1, 11)]*4+[(1, 99)] if failure == "named-root" else None, return_value=(1, 11)))
                stack.enter_context(patch.object(bootstrap, "_names", return_value=[bootstrap.RECORD_NAME]))
                stack.enter_context(patch.object(bootstrap, "_file", side_effect=
                    [(raw,len(raw)),(raw+b" ",len(raw)+1)] if failure == "record-drift" else None,
                    return_value=(raw,len(raw))))
                stack.enter_context(patch.object(bootstrap, "_inventory", return_value=
                    (EXPECTED["expected_digest"], 2 if failure == "inventory" else 3, 3)))
                closing = stack.enter_context(patch.object(bootstrap.os, "close"))
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.authenticate_fixed_kit(**EXPECTED)
                self.assertIn(10, [call.args[0] for call in closing.call_args_list])

    def test_inventory_hashes_original_listing_including_empty_files_and_enforces_global_caps(self):
        tree = {0: {"src": 1, "site": 2, "requirements": 3},
                1: {"empty.py": b""}, 2: {"site.txt": b"site"}, 3: {"lock.txt": b"lock"}}
        def names(fd, cap):
            result = sorted(tree[fd])
            bootstrap._require(len(result) <= cap)
            return result
        def info(name, *, dir_fd, **kwargs):
            child = tree[dir_fd][name]
            return SimpleNamespace(st_mode=stat.S_IFDIR if type(child) is int else stat.S_IFREG)
        def opening(name, flags, *, dir_fd):
            return tree[dir_fd][name]
        def file(fd, name, cap, **kwargs):
            data = tree[fd][name]
            bootstrap._require(len(data) <= cap)
            return hashlib.sha256(data).hexdigest(), len(data)
        with ExitStack() as stack:
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
                stack.enter_context(patch.object(bootstrap.os, name, 0, create=True))
            stack.enter_context(patch.object(bootstrap, "_private", return_value=(1, 11)))
            stack.enter_context(patch.object(bootstrap, "_stamp", return_value=(1, 11)))
            stack.enter_context(patch.object(bootstrap, "_names", side_effect=names))
            stack.enter_context(patch.object(bootstrap, "_file", side_effect=file))
            stack.enter_context(patch.object(bootstrap.os, "stat", side_effect=info))
            stack.enter_context(patch.object(bootstrap.os, "fstat"))
            stack.enter_context(patch.object(bootstrap.os, "open", side_effect=opening))
            closing = stack.enter_context(patch.object(bootstrap.os, "close"))
            listing = "".join(f"{hashlib.sha256(data).hexdigest()}  ./{top}/{name}\n"
                              for top, fd in sorted(tree[0].items()) for name, data in sorted(tree[fd].items()))
            self.assertEqual(bootstrap._inventory(0), ("sha256:"+hashlib.sha256(listing.encode()).hexdigest(),3,8))
            self.assertEqual(sorted(call.args[0] for call in closing.call_args_list), [1,2,3])
            for bound, cap in (("MAX_FILES",2), ("MAX_ENTRIES",5), ("MAX_BYTES",7)):
                with self.subTest(bound=bound), patch.object(bootstrap,bound,cap), self.assertRaises(bootstrap.BootstrapError):
                    bootstrap._inventory(0)
            tree[1]["poison.pth"] = b""
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap._inventory(0)

    def test_file_reader_bounds_bytes_rejects_changed_named_identity_and_closes_fd(self):
        info = SimpleNamespace(st_dev=1, st_ino=11, st_mode=stat.S_IFREG|0o600, st_uid=0,
                               st_gid=0, st_nlink=1, st_size=2, st_mtime_ns=1, st_ctime_ns=1)
        for failure in (None,"growth","named-drift"):
            with self.subTest(failure=failure), ExitStack() as stack:
                for name in ("O_NOFOLLOW","O_NONBLOCK","O_CLOEXEC"):
                    stack.enter_context(patch.object(bootstrap.os,name,0,create=True))
                drift = copy.copy(info)
                drift.st_ino += 1
                stack.enter_context(patch.object(bootstrap.os,"stat",side_effect=[info,drift if failure=="named-drift" else info]))
                stack.enter_context(patch.object(bootstrap.os,"open",return_value=7))
                stack.enter_context(patch.object(bootstrap,"_private",return_value=bootstrap._stamp(info)))
                reading = stack.enter_context(patch.object(bootstrap.os,"read",side_effect=[b"abc" if failure=="growth" else b"ab",b""]))
                closing = stack.enter_context(patch.object(bootstrap.os,"close"))
                if failure:
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap._file(3,"leaf",2,collect=True)
                else:
                    self.assertEqual(bootstrap._file(3,"leaf",2,collect=True),(b"ab",2))
                    self.assertEqual([call.args[1] for call in reading.call_args_list],[3,1])
                closing.assert_called_once_with(7)
