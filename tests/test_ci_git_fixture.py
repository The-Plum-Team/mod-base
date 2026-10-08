"""Known authored SHA-1 object/index fixtures; commands here run as the local test user."""

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from mod_base.build_ci.worker_git import _configuration


def git_fixture() -> tuple[str, str, dict[str, bytes]]:
    objects = {}
    def object_id(kind, data):
        raw=kind.encode()+b' '+str(len(data)).encode()+b'\0'+data
        sha=hashlib.sha1(raw).hexdigest();objects['objects/'+sha[:2]+'/'+sha[2:]]=zlib.compress(raw)
        return sha
    blob=object_id('blob',b'known tracked bytes\n')
    tree=object_id('tree',b'100644 file\0'+bytes.fromhex(blob))
    commit=object_id('commit',('tree '+tree+'\nauthor Fixture <fixture@example.invalid> 1 +0000\n'
                             'committer Fixture <fixture@example.invalid> 1 +0000\n\nauthored fixture\n').encode())
    entry=struct.pack('>10I',0,0,0,0,0,0,0o100644,0,0,len(b'known tracked bytes\n'))+bytes.fromhex(blob)+struct.pack('>H',4)+b'file\0'
    entry+=b'\0'*((-len(entry))%8)
    index=b'DIRC'+struct.pack('>II',2,1)+entry
    objects['index']=index+hashlib.sha1(index).digest()
    objects['refs/tags/v1']=(commit+'\n').encode()
    return commit,tree,objects


class GitFixtureTests(unittest.TestCase):
    def test_generated_config_and_authored_metadata_support_tree_index_clean_and_tag_reads(self):
        executable=shutil.which('git')
        if executable is None: self.skipTest('git is unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);meta=root/'.git';meta.mkdir();(meta/'objects').mkdir();(meta/'refs').mkdir()
            commit,tree,files=git_fixture()
            for name,data in {**files,'HEAD':(commit+'\n').encode(),'config':_configuration('owner/project')}.items():
                leaf=meta/name;leaf.parent.mkdir(parents=True,exist_ok=True);leaf.write_bytes(data)
            (root/'file').write_bytes(b'known tracked bytes\n')
            environment={'PATH':str(Path(executable).parent),'HOME':str(root),'LC_ALL':'C',
                         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_TERMINAL_PROMPT':'0'}
            if os.name=='nt':environment['SYSTEMROOT']=os.environ['SYSTEMROOT']
            def run(*args):
                result=subprocess.run([executable,'-C',str(root),*args],env=environment,stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,check=False)
                self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
                return result.stdout.strip()
            self.assertEqual(tree.encode(),run('rev-parse','HEAD^{tree}'))
            self.assertEqual(commit.encode(),run('rev-parse','HEAD'))
            self.assertEqual(b'',run('status','--porcelain=v1','--untracked-files=all'))
            self.assertEqual(b'v1',run('describe','--tags','--always'))
