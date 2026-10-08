"""Preparation ordering, API brackets and retained-root failure handling with explicit seams."""

import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import worker_preparation as prep
from mod_base.build_ci.source import GitSourceEntry
from mod_base.errors import MbError
from mod_base.pin import Pin
from tests.test_ci_gradle_cache import ACCOUNT
from tests.helpers import ci_stat as info
from tests.test_ci_host import BOUNDARY


SOURCE=Path('/home/runner/candidate')
SEED=Path('/home/runner/gradle-seed')
OVERLAY=Path('/home/runner/kit-overlay')
PIN=Pin('a'*40,'v1.0.3',())
DIGEST='sha256:'+'b'*64
IDENTITY={'repository':'owner/project','tested_sha':'c'*40}
INVENTORY=(GitSourceEntry('file','100644',0,'d'*40),)
RECORDS=[{'path':'file','mode':'100644','size':0,'sha256':'e'*64,'git_blob':'d'*40}]


class WorkerPreparationTests(unittest.TestCase):
    def seams(self, stack):
        events=[]
        host=stack.enter_context(patch.object(prep,'authenticate_privileged_host_boundary'))
        account=stack.enter_context(patch.object(prep,'authenticate_worker_account',return_value=ACCOUNT))
        stack.enter_context(patch.object(prep,'_quiet'))
        terminate=stack.enter_context(patch.object(prep,'terminate_worker'))
        inventory=stack.enter_context(patch.object(prep,'authenticate_source_inventory',return_value=INVENTORY))
        source=stack.enter_context(patch.object(prep,'verify_source_copy',return_value=RECORDS))
        overlay=stack.enter_context(patch.object(prep,'_admit',return_value=RECORDS))
        release=stack.enter_context(patch.object(prep,'verify_released'))
        api=stack.enter_context(patch.object(prep,'authenticate_source_identity'))
        copies={}
        for name in ('source','git','cache','overlay'):
            function={'source':'stage_privileged_worker_source','git':'stage_privileged_worker_git',
                      'cache':'stage_privileged_gradle_cache','overlay':'stage_privileged_worker_overlay'}[name]
            def invoke(*a,stage_name=name,**kw):events.append(stage_name);return RECORDS
            copies[name]=stack.enter_context(patch.object(prep,function,side_effect=invoke))
        data=stack.enter_context(patch.object(prep,'regular_data_records',return_value=RECORDS))
        access=stack.enter_context(patch.object(prep,'authenticate_tree_private_access'))
        mapping={'candidate':7,'gradle-seed':9,'kit-overlay':10,'gradle-home':11,'repository':12,'mod-base-kit':14}
        def directory(parts):
            if parts[-1]=='.git':return 8 if parts[-2]=='candidate' else 13
            return mapping[parts[-1]]
        stats={fd:info(inode=fd,uid=BOUNDARY.uid) for fd in (7,8,9,10)}
        stats.update({fd:info(inode=fd,uid=ACCOUNT.uid,gid=ACCOUNT.gid,mode=stat.S_IFDIR|(0o755 if fd==14 else 0o700)) for fd in (11,12,13,14)})
        stack.enter_context(patch.object(prep,'_open_directory',side_effect=directory))
        stack.enter_context(patch.object(prep.os,'fstat',side_effect=lambda fd:stats[fd]))
        stack.enter_context(patch.object(prep.os,'close'))
        return events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats

    def invoke(self, **extra):
        return prep.prepare_privileged_worker_checkout(object(),extra.pop('source',SOURCE),extra.pop('seed',SEED),extra.pop('overlay',OVERLAY),
            boundary=BOUNDARY,account=ACCOUNT,identity=IDENTITY,pin=extra.pop('pin',PIN),
            expected_digest=extra.pop('expected_digest',DIGEST),**extra)

    def test_malformed_pin_and_digest_reject_before_source_access(self):
        for arguments in ({'pin':object()}, {'pin':Pin('bad','v1.0.3',())},
                          {'pin':Pin('a'*40,'1.0.3',())}, {'pin':Pin('a'*40,'vbad',())},
                          {'expected_digest':object()}, {'expected_digest':'sha256:bad'}):
            with self.subTest(arguments=arguments),ExitStack() as stack:
                events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
                with self.assertRaises(MbError):self.invoke(**arguments)
                inventory.assert_not_called()
                source.assert_not_called()
                overlay.assert_not_called()
                self.assertFalse(events)
                terminate.assert_called_once_with(ACCOUNT)

    def test_complete_order_uses_api_inventory_same_checkout_git_and_final_brackets(self):
        with ExitStack() as stack:
            events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
            self.assertEqual({'source':RECORDS,'git':RECORDS,'gradle':RECORDS,'overlay':RECORDS},self.invoke())
            self.assertEqual(['source','git','cache','overlay'],events)
            self.assertEqual(SOURCE/'.git',copies['git'].call_args.args[0])
            self.assertEqual(INVENTORY,copies['source'].call_args.kwargs['inventory'])
            self.assertEqual((prep.OVERLAY_PATH,),source.call_args_list[1].kwargs['generated_roots'])
            self.assertEqual(2,release.call_count)
            api.assert_called_once()
            self.assertEqual(2,access.call_count)
            terminate.assert_not_called()

    def test_stage_failures_never_continue_and_lock_worker(self):
        for failed in ('source','git','cache','overlay'):
            with self.subTest(stage=failed),ExitStack() as stack:
                events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
                copies[failed].side_effect=MbError('fixture failure')
                with self.assertRaises(MbError):self.invoke()
                names=['source','git','cache','overlay']
                for name in names[names.index(failed)+1:]:copies[name].assert_not_called()
                terminate.assert_called_once_with(ACCOUNT)

    def test_final_source_cache_git_overlay_or_api_drift_rejects(self):
        for kind in ('source','git','cache','overlay','api','original'):
            with self.subTest(kind=kind),ExitStack() as stack:
                events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
                if kind=='source':source.side_effect=[RECORDS,[]]
                elif kind=='git':data.side_effect=[[]]
                elif kind=='cache':data.side_effect=[RECORDS,[]]
                elif kind=='overlay':overlay.side_effect=[RECORDS,[]]
                elif kind=='api':api.side_effect=MbError('moved source')
                else:
                    def mutate(*a,**kw):stats[8]=info(inode=80,uid=BOUNDARY.uid);return RECORDS
                    copies['overlay'].side_effect=mutate
                with self.assertRaises(MbError):self.invoke()
                terminate.assert_called_once_with(ACCOUNT)

    def test_retained_source_git_cache_or_overlay_replacement_rejects(self):
        for fd in (11,12,13):
            with self.subTest(fd=fd),ExitStack() as stack:
                events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
                def mutate(*a,**kw):
                    stats[fd]=info(inode=80,uid=ACCOUNT.uid,gid=ACCOUNT.gid,mode=stat.S_IFDIR|0o700);return RECORDS
                copies['overlay'].side_effect=mutate
                with self.assertRaises(MbError):self.invoke()
                terminate.assert_called_once_with(ACCOUNT)

    def test_bad_paths_root_identity_and_overlay_collision_reject_before_staging(self):
        for kind in ('outside','overlap','root','account','collision'):
            with self.subTest(kind=kind),ExitStack() as stack:
                events,host,account,terminate,inventory,source,overlay,release,api,copies,data,access,stats=self.seams(stack)
                kwargs={}
                if kind=='outside':kwargs['seed']=Path('/tmp/outside')
                elif kind=='overlap':kwargs['overlay']=SOURCE/'out/mod-base-kit'
                elif kind=='root':host.side_effect=MbError('root')
                elif kind=='account':account.return_value=object()
                else:inventory.return_value=(GitSourceEntry('out/mod-base-kit/file','100644',0,'d'*40),)
                with self.assertRaises(MbError):self.invoke(**kwargs)
                self.assertFalse(events)
