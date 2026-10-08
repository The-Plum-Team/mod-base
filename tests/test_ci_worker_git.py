"""Git-data curation and staging with explicit OS/copy seams; no privileged Git execution."""

import hashlib
import stat
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import worker_git as git
from mod_base.build_ci.worker import WorkerError
from mod_base.errors import MbError
from tests.test_ci_gradle_cache import ACCOUNT
from tests.test_ci_python_installation import BOUNDARY, info


ROOT = Path('/home/runner/metadata')
COMMIT = 'a' * 40
PATHS = ('index', 'objects/ab/' + 'c' * 38)
RECORDS = [{'path': p, 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()} for p in PATHS]


class GitDataTests(unittest.TestCase):
    def test_fixed_config_has_no_source_credentials_filters_includes_or_hooks(self):
        data = git._configuration('owner/project')
        self.assertIn(b'https://github.com/owner/project.git', data)
        self.assertIn(b'hooksPath = /dev/null', data)
        self.assertIn(b'fsmonitor = false', data)
        self.assertNotIn(b'credential', data)
        for repository in ('owner/repo\n[include]', 'owner/repo/token', 'https://host/repo'):
            with self.assertRaises(MbError): git._configuration(repository)

    def test_ref_texts_accept_basic_clone_shapes_and_reject_redirects_replace_or_duplicates(self):
        valid = {'refs/remotes/origin/HEAD': b'ref: refs/remotes/origin/main\n',
                 'refs/heads/main': (COMMIT+'\n').encode(), 'shallow': (COMMIT+'\n').encode(),
                 'packed-refs': ('# pack-refs with: peeled fully-peeled sorted \n'+COMMIT+' refs/tags/v1\n^'+'b'*40+'\n').encode()}
        with patch.object(git, 'read_child_file', side_effect=lambda root, path, **kw: valid[path]):
            git._texts(ROOT, tuple(valid))
        cases = [('refs/heads/main', b'ref: /outside\n'), ('refs/heads/main', b'a'*40+b'\nextra'),
                 ('shallow', (COMMIT+'\n'+COMMIT+'\n').encode()),
                 ('packed-refs', (COMMIT+' refs/replace/'+COMMIT+'\n').encode()),
                 ('packed-refs', (COMMIT+' refs/tags/v1\n'+COMMIT+' refs/tags/v1\n').encode()),
                 ('packed-refs', b'^'+b'a'*40+b'\n'), ('packed-refs', b'# unsupported\n'),
                 ('packed-refs', (COMMIT+' refs/tags/v1').encode())]
        for path, raw in cases:
            with self.subTest(path=path, raw=raw), patch.object(git, 'read_child_file', return_value=raw), self.assertRaises(MbError):
                git._texts(ROOT, (path,))
        for path in ('shallow','packed-refs'):
            with self.subTest(path=path), patch.object(git.limits,'MAX_CI_GIT_METADATA_FILES',1), \
                    patch.object(git,'read_child_file',return_value=b'\n'*4), self.assertRaises(MbError):
                git._texts(ROOT,(path,))
        for raw in (b'# pack-refs with:'+b' peeled'*600+b'\n',
                    b'# pack-refs with: peeled\vfully-peeled\nsorted\n',
                    b'# pack-refs with: peeled\tfully-peeled\n', b'\r'*1000+b'\n'):
            with self.subTest(raw=raw[:40]),patch.object(git,'read_child_file',return_value=raw),self.assertRaises(MbError):
                git._texts(ROOT,('packed-refs',))

    def selection(self, *, kind=None):
        nodes = {7: info(inode=7, uid=BOUNDARY.uid), 8: info(inode=8, uid=BOUNDARY.uid),
                 9: info(inode=9, uid=BOUNDARY.uid), 10: info(inode=10, uid=BOUNDARY.uid)}
        listing = {7: ['HEAD', 'config', 'hooks', 'index', 'objects'], 8: ['ab'], 9: ['c'*38], 10: ['dangerous-hook']}
        edges = {(7,'objects'):8, (8,'ab'):9, (7,'hooks'):10}
        def metadata(name, *, dir_fd, follow_symlinks):
            if (dir_fd,name) in edges: return nodes[edges[(dir_fd,name)]]
            mode = stat.S_IFLNK | 0o777 if kind == 'link' and name == 'index' else stat.S_IFREG | 0o644
            return info(inode=20, uid=ACCOUNT.uid if kind == 'foreign' and name == 'index' else BOUNDARY.uid, mode=mode)
        if kind == 'shared': listing[7].append('commondir')
        if kind == 'alternates': listing[8].append('info');edges[(8,'info')]=11;nodes[11]=info(inode=11,uid=BOUNDARY.uid);listing[11]=['alternates']
        if kind == 'promisor': listing[9].append('unapproved.promisor')
        if kind == 'alias': listing[7].append('CONFIG')
        if kind == 'missing': listing[7].remove('HEAD')
        with ExitStack() as stack:
            stack.enter_context(patch.object(git, '_open_directory', side_effect=lambda parts, root=None: 7 if root is None else edges[(root,parts[0])]))
            stack.enter_context(patch.object(git.os, 'fstat', side_effect=lambda fd:nodes[fd]))
            stack.enter_context(patch.object(git.os, 'stat', side_effect=metadata))
            stack.enter_context(patch.object(git.os, 'scandir', side_effect=lambda fd:nullcontext(iter(SimpleNamespace(name=n) for n in listing[fd]))))
            stack.enter_context(patch.object(git.os, 'close'))
            return git._selection(ROOT, BOUNDARY)

    def test_selection_omits_hooks_config_head_and_refuses_unsafe_or_shared_layouts(self):
        self.assertEqual(PATHS, self.selection())
        for kind in ('link','foreign','shared','alternates','promisor','alias','missing'):
            with self.subTest(kind=kind), self.assertRaises(MbError): self.selection(kind=kind)


class WorkerGitTests(unittest.TestCase):
    def seams(self, stack):
        host = stack.enter_context(patch.object(git,'authenticate_privileged_host_boundary'))
        stack.enter_context(patch.object(git,'authenticate_worker_account',return_value=ACCOUNT))
        stack.enter_context(patch.object(git,'_quiet'))
        terminate = stack.enter_context(patch.object(git,'terminate_worker'))
        selection = stack.enter_context(patch.object(git,'_selection',return_value=PATHS))
        stack.enter_context(patch.object(git,'_texts'))
        records = stack.enter_context(patch.object(git,'selected_regular_data_records',return_value=RECORDS))
        copy = stack.enter_context(patch.object(git,'copy_selected_regular_data_files',return_value=RECORDS))
        packet = {'HEAD':(COMMIT+'\n').encode(),'config':git._configuration('owner/project')}
        final = sorted(RECORDS+[{'path':n,'size':len(d),'sha256':hashlib.sha256(d).hexdigest()} for n,d in packet.items()],key=lambda r:r['path'])
        data = stack.enter_context(patch.object(git,'regular_data_records',return_value=final))
        private = stack.enter_context(patch.object(git,'privatize_regular_data_copy',return_value=final))
        access = stack.enter_context(patch.object(git,'authenticate_tree_private_access'))
        writes = stack.enter_context(patch.object(git,'write_new'))
        stats={7:info(inode=7,uid=BOUNDARY.uid),8:info(inode=8,uid=ACCOUNT.uid,gid=ACCOUNT.gid,mode=stat.S_IFDIR|0o700),9:info(inode=9)}
        stack.enter_context(patch.object(git,'_open_directory',side_effect=lambda parts:7 if parts[-1]=='metadata' else 8 if parts[-1]=='repository' else 9))
        stack.enter_context(patch.object(git.os,'fstat',side_effect=lambda fd:stats[fd]))
        for name in ('close','mkdir','fchown','fchmod'): stack.enter_context(patch.object(git.os,name,create=True))
        def publish(path, writer):
            self.assertEqual(str(git.WORKER_ROOT/'repository/.git'),path.as_posix())
            return writer(path.parent/'.git.building-fixture',9)
        stack.enter_context(patch.object(git,'atomic_directory',side_effect=publish))
        return host,terminate,selection,records,copy,data,private,access,writes,stats,final

    def test_only_selected_data_copies_and_generated_head_config_are_private(self):
        with ExitStack() as stack:
            host,terminate,selection,records,copy,data,private,access,writes,stats,final=self.seams(stack)
            self.assertEqual(final,git.stage_privileged_worker_git(ROOT,boundary=BOUNDARY,account=ACCOUNT,repository='owner/project',tested_commit=COMMIT))
            copy.assert_called_once_with(ROOT,9,paths=PATHS,**git._BOUNDS)
            self.assertEqual(['HEAD','config'],[call.args[1] for call in writes.call_args_list])
            self.assertEqual(2,access.call_count)
            terminate.assert_not_called()

    def test_copy_handoff_source_parent_and_access_failures_lock_worker(self):
        for kind in ('copy','private','source','parent','access','os'):
            with self.subTest(kind=kind),ExitStack() as stack:
                host,terminate,selection,records,copy,data,private,access,writes,stats,final=self.seams(stack)
                if kind=='copy':copy.return_value=[]
                elif kind=='private':private.return_value=[]
                elif kind=='source':records.side_effect=[RECORDS,[]]
                elif kind=='parent':
                    def mutate(*a,**kw):stats[8]=info(inode=80,uid=ACCOUNT.uid,gid=ACCOUNT.gid,mode=stat.S_IFDIR|0o700);return RECORDS
                    copy.side_effect=mutate
                elif kind=='access':access.side_effect=WorkerError('access')
                else:copy.side_effect=OSError('read')
                with self.assertRaises(MbError):git.stage_privileged_worker_git(ROOT,boundary=BOUNDARY,account=ACCOUNT,repository='owner/project',tested_commit=COMMIT)
                terminate.assert_called_once_with(ACCOUNT)

    def test_invalid_commit_repository_role_or_path_never_copies(self):
        for kind in ('commit','repository','role','path'):
            with self.subTest(kind=kind),ExitStack() as stack:
                host,terminate,selection,records,copy,data,private,access,writes,stats,final=self.seams(stack)
                root,repo,commit=ROOT,'owner/project',COMMIT
                if kind=='commit':commit='bad'
                elif kind=='repository':repo='owner/project\nsecret'
                elif kind=='role':host.side_effect=WorkerError('role')
                else:root=Path('/tmp/outside')
                with self.assertRaises(MbError):git.stage_privileged_worker_git(root,boundary=BOUNDARY,account=ACCOUNT,repository=repo,tested_commit=commit)
                copy.assert_not_called()
