"""Approved synthetic tool bytes; explicit Windows syscall seams are not Linux enrollment."""

import hashlib
import stat
import unittest
from unittest.mock import patch

from mod_base.build_ci import toolchain
from mod_base.build_ci.worker import WorkerAccount, WorkerResult
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests.test_ci_toolchain import Filesystem, ROOT, BOUNDARY


class BytesFilesystem(Filesystem):
    def __init__(self):
        super().__init__()
        self.data = {ROOT+"/bin/python3.11":b"bin", ROOT+"/lib/site.py":b"hello"}
        self.offsets = {}
        self.read_effect = None

    def opening(self,name,flags,*,dir_fd=None):
        path = self.path(name,dir_fd)
        if stat.S_ISREG(self.nodes[path].st_mode):
            descriptor=self.next_fd;self.next_fd+=1
            self.descriptors[descriptor]=path;self.offsets[descriptor]=0
            return descriptor
        return super().opening(name,flags,dir_fd=dir_fd)

    def reading(self,descriptor,size):
        path=self.descriptors[descriptor]
        if self.read_effect is not None:self.read_effect(path)
        offset=self.offsets[descriptor]
        result=self.data[path][offset:offset+size]
        self.offsets[descriptor]+=len(result)
        return result

    def approved_digest(self):
        # Independent fixture bytes and row construction, not a production acceptance generator.
        digest=hashlib.sha256(canonical_json({"format":"mod-base.tool-bytes-v1","roots":[ROOT]}))
        for path,node in sorted(self.nodes.items()):
            row={"path":path,"mode":stat.S_IMODE(node.st_mode)}
            if stat.S_ISREG(node.st_mode):
                row.update(type="file",size=len(self.data[path]),sha256=hashlib.sha256(self.data[path]).hexdigest())
            elif stat.S_ISLNK(node.st_mode):row.update(type="link",target=node.target)
            else:row.update(type="directory")
            digest.update(canonical_json(row))
        return "sha256:"+digest.hexdigest()

    def admit(self,proof,expected,*,privileged=False):
        with patch.object(toolchain.os,"read",side_effect=self.reading), \
                patch.object(toolchain.os,"O_NONBLOCK",0,create=True), \
                patch.object(toolchain,"authenticate_privileged_host_boundary"):
            return self.inspect(operation=lambda:toolchain.authenticate_toolchain_bytes(
                proof,boundary=BOUNDARY,expected_digest=expected,privileged=privileged))


class ToolBytesTests(unittest.TestCase):
    def test_byte_fenced_foreign_accounts_cannot_launch_or_authorize_cleanup(self):
        account=WorkerAccount('worker',2001,2001,'/tmp/private')
        for supplied,actual in ((object(),account),(account,object()),
                               (WorkerAccount('worker',BOUNDARY.uid,121,'/tmp/private'),
                                WorkerAccount('worker',BOUNDARY.uid,121,'/tmp/private'))):
            with self.subTest(supplied=supplied),patch.object(toolchain,'authenticate_host_boundary'), \
                    patch.object(toolchain,'authenticate_worker_account',return_value=actual), \
                    patch.object(toolchain,'authenticate_toolchain_bytes') as reading, \
                    patch.object(toolchain,'execute_isolated_worker') as launch, \
                    patch.object(toolchain,'terminate_worker') as terminate:
                with self.assertRaises(MbError):
                    toolchain.execute_byte_fenced_worker(supplied,boundary=BOUNDARY,tools=object(),
                        expected_digest='sha256:'+'a'*64,command=(),python=ROOT+'/bin/python3',
                        java_home=None,identity={},run_id=42,run_attempt=2,values={},timeout_seconds=60)
                reading.assert_not_called();launch.assert_not_called();terminate.assert_not_called()

    def execute(self, fs, proof, expected, launch):
        account=WorkerAccount('worker',2001,2001,'/tmp/private')
        with patch.object(toolchain,'authenticate_worker_account',return_value=account), \
                patch.object(toolchain.os,'read',side_effect=fs.reading), \
                patch.object(toolchain.os,'O_NONBLOCK',0,create=True), \
                patch.object(toolchain,'execute_isolated_worker',side_effect=launch):
            return fs.inspect(operation=lambda:toolchain.execute_byte_fenced_worker(
                account,boundary=BOUNDARY,tools=proof,expected_digest=expected,
                command=(ROOT+'/bin/python3','-I','-B','/tmp/private/scripts/ci/dispatch.py'),
                python=ROOT+'/bin/python3',java_home=None,identity={},run_id=42,run_attempt=2,
                values={},timeout_seconds=60))

    def test_byte_fenced_execution_rechecks_full_bytes_around_existing_path_fence(self):
        fs=BytesFilesystem();metadata=fs.inspect();expected=fs.approved_digest()
        proof=fs.admit(metadata,expected);events=[]
        original=toolchain._tool_file_digest
        def reading(*args):events.append('bytes');return original(*args)
        result=WorkerResult(0,b'bounded fixture output',False)
        def launch(*args,**kwargs):events.append('execute');return result
        with patch.object(toolchain,'_tool_file_digest',side_effect=reading), \
                patch.object(toolchain,'terminate_worker') as terminate:
            self.assertEqual(self.execute(fs,proof,expected,launch),result)
        self.assertEqual(events,['bytes','bytes','execute','bytes','bytes'])
        terminate.assert_not_called()
        self.assertEqual(fs.descriptors,{})

    def test_byte_fenced_pre_and_post_execution_same_metadata_byte_drift_rejects(self):
        for phase in ('before','after'):
            fs=BytesFilesystem();metadata=fs.inspect();expected=fs.approved_digest()
            proof=fs.admit(metadata,expected);calls=[]
            def launch(*args,**kwargs):
                calls.append('execute');fs.data[ROOT+'/lib/site.py']=b'other'
                return WorkerResult(0,b'',False)
            if phase=='before':fs.data[ROOT+'/lib/site.py']=b'other'
            with self.subTest(phase=phase),patch.object(toolchain,'terminate_worker') as terminate:
                with self.assertRaises(MbError):self.execute(fs,proof,expected,launch)
                terminate.assert_called_once()
            self.assertEqual(calls,[] if phase=='before' else ['execute'])
            self.assertEqual(fs.descriptors,{})

    def test_byte_fenced_invalid_proof_independent_digest_and_execution_failure_close_account(self):
        fs=BytesFilesystem();metadata=fs.inspect();expected=fs.approved_digest();proof=fs.admit(metadata,expected)
        for retained,approved in ((object(),expected),(proof,'sha256:'+'f'*64),(proof,object()),
                                   (toolchain.ToolBytesProof(object(),expected),expected)):
            with self.subTest(retained=retained),patch.object(toolchain,'terminate_worker') as terminate:
                with self.assertRaises(MbError):self.execute(fs,retained,approved,lambda *a,**kw:self.fail('must not launch'))
                terminate.assert_called_once()
        def failing(*args,**kwargs):raise MbError('execution failed')
        with patch.object(toolchain,'terminate_worker') as terminate,self.assertRaises(MbError):
            self.execute(fs,proof,expected,failing)
        terminate.assert_called_once()
        self.assertEqual(fs.descriptors,{})

    def test_full_bytes_digest_preserves_alias_empty_file_and_role_bound_metadata(self):
        fs=BytesFilesystem()
        path=ROOT+"/lib/empty.py";fs.add(path,stat.S_IFREG|0o644);fs.data[path]=b""
        proof=fs.inspect();expected=fs.approved_digest()
        for privileged in (False,True):
            admitted=fs.admit(proof,expected,privileged=privileged)
            self.assertEqual(admitted,toolchain.ToolBytesProof(proof,expected))
            self.assertEqual(fs.descriptors,{})

    def test_same_metadata_changed_bytes_and_wrong_expected_digest_fail_closed(self):
        fs=BytesFilesystem();proof=fs.inspect();expected=fs.approved_digest()
        fs.data[ROOT+"/lib/site.py"]=b"other"
        for approved in (expected,"sha256:"+"f"*64):
            with self.subTest(expected=approved),self.assertRaises(MbError):fs.admit(proof,approved)
            self.assertEqual(fs.descriptors,{})

    def test_links_modes_paths_and_selected_roots_are_part_of_byte_contract(self):
        fs=BytesFilesystem();original=fs.approved_digest()
        for change in ("mode","alias","path"):
            altered=BytesFilesystem()
            if change=="mode":altered.nodes[ROOT+"/lib/site.py"].st_mode=stat.S_IFREG|0o600
            elif change=="alias":altered.nodes[ROOT+"/bin/python3"].target="./python3.11"
            else:
                altered.add(ROOT+"/lib/extra",stat.S_IFREG|0o644,size=1)
                altered.data[ROOT+"/lib/extra"]=b"x"
            if change=="alias":altered.nodes[ROOT+"/bin/python3"].st_size=len("./python3.11")
            with self.subTest(change=change),self.assertRaises(MbError):altered.admit(altered.inspect(),original)

    def test_hard_links_short_growing_changed_and_io_reads_close_every_descriptor(self):
        for change in ("hard-link","short","grow","stamp","io"):
            fs=BytesFilesystem();path=ROOT+"/lib/site.py";expected=fs.approved_digest()
            if change=="hard-link":fs.nodes[path].st_nlink=2
            proof=fs.inspect()
            if change=="short":fs.data[path]=b"hi"
            elif change=="grow":fs.data[path]+=b"!"
            elif change=="stamp":fs.read_effect=lambda path: setattr(fs.nodes[path],"st_ctime_ns",2)
            elif change=="io":
                def failing(path):raise OSError("fixture read")
                fs.read_effect=failing
            with self.subTest(change=change),self.assertRaises(MbError):fs.admit(proof,expected)
            self.assertEqual(fs.descriptors,{})

    def test_role_and_invalid_retained_proof_precede_byte_io(self):
        fs=BytesFilesystem();proof=fs.inspect();expected=fs.approved_digest()
        with patch.object(toolchain,"authenticate_host_boundary",side_effect=MbError("role")), \
                patch.object(toolchain.os,"read") as reading,self.assertRaises(MbError):
            toolchain.authenticate_toolchain_bytes(proof,boundary=BOUNDARY,expected_digest=expected)
        reading.assert_not_called()
        for changed in ({},toolchain.ToolTreeProof(proof.roots,"f"*64,proof.files,proof.entries,proof.total_bytes)):
            with self.subTest(proof=changed),self.assertRaises(MbError):fs.admit(changed,expected)
        with self.assertRaises(MbError):fs.admit(proof,expected,privileged="root")

    def test_post_read_metadata_drift_rejects_even_matching_bytes(self):
        fs=BytesFilesystem();proof=fs.inspect();expected=fs.approved_digest()
        original=toolchain._tool_file_digest
        def changed(path,stamp):
            value=original(path,stamp)
            fs.nodes[ROOT+"/lib"].st_ctime_ns=2
            return value
        with patch.object(toolchain,"_tool_file_digest",side_effect=changed),self.assertRaises(MbError):
            fs.admit(proof,expected)
        self.assertEqual(fs.descriptors,{})
