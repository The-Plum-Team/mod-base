"""Fixed lock-bound program installation; explicit Windows seams, no physical Linux claim."""

import hashlib
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import bootstrap_installation as bootstrap
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.host import HostBoundary
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.pin import MAX_LOCK_BYTES
from mod_base.runtime import build_invocation


BOUNDARY = HostBoundary("/home/runner",1001,121,1,10,0o755)
SOURCE = Path("/home/runner/approved-kit")
RAW = b"# inert program bytes, never executed\n"
DIGEST = hashlib.sha256(RAW).hexdigest()
LOCK = (DIGEST + "  ./tools/ci_privileged_bootstrap.py\n").encode()
KIT = KitInstallation("a"*40,"1.0.3","sha256:"+"b"*64,3,3,1,11)
PROGRAM = bootstrap.BootstrapInstallation(DIGEST,len(RAW),1,12)


def invocation():
    return build_invocation(Path(__file__).parent / "fixtures/mods/qs_like",None,
                            {"MOD_BASE_KIT_SHA":"a"*40},check_repository=False,root=SOURCE)


class BootstrapInstallationTests(unittest.TestCase):
    def test_lock_is_closed_sorted_unique_and_requires_exact_fixed_program(self):
        self.assertEqual(limits.MAX_CI_BOOTSTRAP_LOCK_BYTES, MAX_LOCK_BYTES)
        with patch.object(bootstrap,"read_child_file",return_value=LOCK):
            self.assertEqual(bootstrap._approved_program_digest(),DIGEST)
        for raw in (LOCK+LOCK,LOCK.replace(b"\n",b"\r\n"),LOCK[:-1],b"\xff",
                    LOCK.replace(b"tools/",b"../tools/"),LOCK.replace(b"tools/",b"actions/"),
                    LOCK.replace(b"ci_privileged_bootstrap.py",b"different.py"),
                    LOCK.replace(DIGEST.encode(),b"x"*64),
                    LOCK+(DIGEST+"  ./template/a\n").encode()):
            with self.subTest(raw=raw[:40]),patch.object(bootstrap,"read_child_file",return_value=raw),self.assertRaises(MbError):
                bootstrap._approved_program_digest()

    def _seams(self, stack):
        host = stack.enter_context(patch.object(bootstrap,"authenticate_privileged_host_boundary"))
        kit = stack.enter_context(patch.object(bootstrap,"authenticate_privileged_kit"))
        stack.enter_context(patch.object(bootstrap,"_layout"))
        stack.enter_context(patch.object(bootstrap,"_approved_program_digest",return_value=DIGEST))
        stack.enter_context(patch.object(bootstrap,"_identity",return_value=(1,12)))
        stack.enter_context(patch.object(bootstrap,"authenticate_tree_private_access"))
        return host,kit

    def test_new_private_fixed_publication_is_bound_to_source_lock_and_retained_root(self):
        supplied = invocation()
        with ExitStack() as stack:
            host,kit = self._seams(stack)
            stack.enter_context(patch.object(bootstrap,"read_child_file",return_value=RAW))
            write = stack.enter_context(patch.object(bootstrap,"write_new"))
            stack.enter_context(patch.object(bootstrap.os,"O_NOFOLLOW",0,create=True))
            stack.enter_context(patch.object(bootstrap.os,"open",return_value=7))
            mode = stack.enter_context(patch.object(bootstrap.os,"fchmod",create=True))
            stack.enter_context(patch.object(bootstrap.os,"fsync"))
            stack.enter_context(patch.object(bootstrap.os,"close"))
            publish = stack.enter_context(patch.object(bootstrap,"atomic_directory",
                side_effect=lambda path,fill: fill(Path("/private-stage"),31)))
            self.assertEqual(bootstrap.install_privileged_bootstrap(supplied,boundary=BOUNDARY,installation=KIT),PROGRAM)
            publish.assert_called_once()
            self.assertEqual(publish.call_args.args[0],Path(str(bootstrap.PRIVILEGED_BOOTSTRAP_ROOT)))
            write.assert_called_once_with(31,"ci_privileged_bootstrap.py",RAW)
            mode.assert_called_once_with(7,0o600)
            self.assertEqual(host.call_count,2)
            self.assertEqual(kit.call_count,2)

    def test_mismatched_program_or_invocation_never_reaches_publication(self):
        for changed in (b"modified source",RAW):
            with self.subTest(changed=changed),ExitStack() as stack:
                self._seams(stack)
                stack.enter_context(patch.object(bootstrap,"read_child_file",return_value=changed))
                publish = stack.enter_context(patch.object(bootstrap,"atomic_directory"))
                supplied = invocation() if changed != RAW else None
                with self.assertRaises(MbError):
                    bootstrap.install_privileged_bootstrap(supplied,boundary=BOUNDARY,installation=KIT)
                publish.assert_not_called()

    def test_changed_stage_or_source_aborts_atomic_publication(self):
        supplied = invocation()
        for reads in ((RAW,b"changed stage"),(RAW,RAW,b"changed source")):
            with self.subTest(reads=reads),ExitStack() as stack:
                self._seams(stack)
                stack.enter_context(patch.object(bootstrap,"read_child_file",side_effect=reads))
                stack.enter_context(patch.object(bootstrap,"write_new"))
                stack.enter_context(patch.object(bootstrap.os,"O_NOFOLLOW",0,create=True))
                stack.enter_context(patch.object(bootstrap.os,"open",return_value=7))
                stack.enter_context(patch.object(bootstrap.os,"fchmod",create=True))
                stack.enter_context(patch.object(bootstrap.os,"fsync"))
                stack.enter_context(patch.object(bootstrap.os,"close"))
                stack.enter_context(patch.object(bootstrap,"atomic_directory",
                    side_effect=lambda path,fill: fill(Path("/private-stage"),31)))
                with self.assertRaises(MbError):
                    bootstrap.install_privileged_bootstrap(supplied,boundary=BOUNDARY,installation=KIT)

    def test_reauthentication_requires_lock_hash_bytes_counts_and_original_inode(self):
        with ExitStack() as stack:
            self._seams(stack)
            reading = stack.enter_context(patch.object(bootstrap,"_read_private_record",return_value=RAW))
            bootstrap.authenticate_privileged_bootstrap(PROGRAM,boundary=BOUNDARY,installation=KIT)
            self.assertEqual(reading.call_count,2)
        for changed in (bootstrap.BootstrapInstallation(DIGEST,True,1,12),
                        bootstrap.BootstrapInstallation(DIGEST,len(RAW),1,13),
                        bootstrap.BootstrapInstallation("c"*64,len(RAW),1,12)):
            with self.subTest(changed=changed),ExitStack() as stack:
                self._seams(stack)
                stack.enter_context(patch.object(bootstrap,"_read_private_record",return_value=RAW))
                with self.assertRaises(MbError):
                    bootstrap.authenticate_privileged_bootstrap(changed,boundary=BOUNDARY,installation=KIT)
        for raws in ((b"changed",b"changed"),(RAW,RAW+b" ")):
            with self.subTest(raws=raws),ExitStack() as stack:
                self._seams(stack)
                stack.enter_context(patch.object(bootstrap,"_read_private_record",side_effect=raws))
                with self.assertRaises(MbError):
                    bootstrap.authenticate_privileged_bootstrap(PROGRAM,boundary=BOUNDARY,installation=KIT)

    def test_host_role_first_and_filesystem_errors_are_normalized(self):
        for operation in (lambda: bootstrap.install_privileged_bootstrap(None,boundary=BOUNDARY,installation=KIT),
                          lambda: bootstrap.authenticate_privileged_bootstrap(PROGRAM,boundary=BOUNDARY,installation=KIT)):
            with patch.object(bootstrap,"authenticate_privileged_host_boundary",side_effect=bootstrap.WorkerError("role")), \
                    patch.object(bootstrap,"authenticate_privileged_kit") as kit,self.assertRaises(MbError):
                operation()
            kit.assert_not_called()
        with ExitStack() as stack:
            self._seams(stack)
            stack.enter_context(patch.object(bootstrap,"read_child_file",side_effect=OSError("fixture")))
            with self.assertRaises(bootstrap.WorkerError):
                bootstrap.install_privileged_bootstrap(invocation(),boundary=BOUNDARY,installation=KIT)
