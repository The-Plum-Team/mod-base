"""Tracked-source staging composition; explicit OS seams confer no hosted execution proof."""

import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import worker_source as source
from mod_base.build_ci.source import GitSourceEntry
from mod_base.build_ci.worker import WorkerError
from tests.test_ci_gradle_cache import ACCOUNT
from tests.helpers import ci_stat as info
from tests.test_ci_host import BOUNDARY


ROOT = Path('/home/runner/candidate')
INVENTORY = (GitSourceEntry('script.sh', '100755', 3, 'a' * 40),)
RECORDS = [{'path': 'script.sh', 'mode': '100755', 'size': 3,
            'git_blob': 'a' * 40, 'sha256': 'b' * 64}]


class WorkerSourceTests(unittest.TestCase):
    def seams(self, stack):
        host = stack.enter_context(patch.object(source, 'authenticate_privileged_host_boundary'))
        account = stack.enter_context(patch.object(source, 'authenticate_worker_account', return_value=ACCOUNT))
        quiet = stack.enter_context(patch.object(source, '_quiet'))
        terminate = stack.enter_context(patch.object(source, 'terminate_worker'))
        verify = stack.enter_context(patch.object(source, 'verify_source_copy', return_value=RECORDS))
        copy = stack.enter_context(patch.object(source, 'materialize_source_copy', return_value=RECORDS))
        stats = {7: info(inode=7, uid=BOUNDARY.uid), 8: info(inode=8, uid=BOUNDARY.uid),
                 9: info(inode=9, uid=0, gid=0, mode=stat.S_IFDIR | 0o700)}
        def handoff(*args, **kwargs):
            stats[9] = info(inode=9, uid=ACCOUNT.uid, gid=ACCOUNT.gid, mode=stat.S_IFDIR | 0o700)
            return RECORDS
        private = stack.enter_context(patch.object(source, 'privatize_source_copy', side_effect=handoff))
        stack.enter_context(patch.object(source, '_open_directory', side_effect=lambda parts:
            7 if parts[-1] == 'candidate' else 9 if parts[-1] == 'repository' else 8))
        stack.enter_context(patch.object(source.os, 'fstat', side_effect=lambda fd: stats[fd]))
        stack.enter_context(patch.object(source.os, 'close'))
        return host, account, quiet, terminate, verify, copy, private, stats

    def test_fixed_exclusive_copy_preserves_full_inventory_and_private_handoff(self):
        with ExitStack() as stack:
            host, account, quiet, terminate, verify, copy, private, stats = self.seams(stack)
            self.assertEqual(RECORDS, source.stage_privileged_worker_source(ROOT,
                boundary=BOUNDARY, account=ACCOUNT, inventory=INVENTORY))
            copy.assert_called_once_with(ROOT, Path(str(source.WORKER_ROOT / 'repository')), inventory=INVENTORY)
            self.assertEqual(('script.sh',), private.call_args.kwargs['tracked_paths'])
            self.assertEqual(0, private.call_args.kwargs['source_owner_uid'])
            self.assertGreaterEqual(quiet.call_count, 4)
            self.assertEqual(3, verify.call_count)
            terminate.assert_not_called()

    def test_bad_role_account_or_path_never_publishes(self):
        for kind in ('role', 'account', 'path'):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, terminate, verify, copy, private, stats = self.seams(stack)
                root = ROOT
                if kind == 'role': host.side_effect = WorkerError('role')
                elif kind == 'account': account.return_value = object()
                else: root = Path('/tmp/outside')
                with self.assertRaises(WorkerError):
                    source.stage_privileged_worker_source(root, boundary=BOUNDARY, account=ACCOUNT, inventory=INVENTORY)
                copy.assert_not_called()

    def test_copy_handoff_source_and_metadata_failures_lock_worker(self):
        for kind in ('copy', 'handoff', 'source', 'root-owner', 'late-owner', 'os'):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, terminate, verify, copy, private, stats = self.seams(stack)
                if kind == 'copy': copy.return_value = []
                elif kind == 'handoff': private.side_effect = None; private.return_value = []
                elif kind == 'source': verify.side_effect = [RECORDS, RECORDS, []]
                elif kind == 'root-owner': stats[9] = info(inode=9, uid=ACCOUNT.uid)
                elif kind == 'late-owner': private.side_effect = None; private.return_value = RECORDS
                else: copy.side_effect = OSError('copy')
                with self.assertRaises(WorkerError):
                    source.stage_privileged_worker_source(ROOT, boundary=BOUNDARY, account=ACCOUNT, inventory=INVENTORY)
                terminate.assert_called_once_with(ACCOUNT)

    def test_source_or_parent_substitution_during_copy_rejects(self):
        for fd in (7, 8):
            with self.subTest(fd=fd), ExitStack() as stack:
                host, account, quiet, terminate, verify, copy, private, stats = self.seams(stack)
                def mutate(*args, **kwargs):
                    stats[fd] = info(inode=70, uid=BOUNDARY.uid)
                    return RECORDS
                copy.side_effect = mutate
                with self.assertRaises(WorkerError):
                    source.stage_privileged_worker_source(ROOT, boundary=BOUNDARY, account=ACCOUNT, inventory=INVENTORY)
                private.assert_not_called()
                terminate.assert_called_once_with(ACCOUNT)
