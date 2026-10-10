"""Immutable controller activation/config binding with inert real fake-API object reads."""

import unittest
from unittest.mock import patch

from mod_base.build_ci import controller
from mod_base.build_ci.activation import ACTIVATION_MODES, ACTIVATION_PATH
from mod_base.errors import MbError
from mod_base.github.contents import tree
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_activation
from tests import test_ci_controller as fixture


class ControllerActivationTests(unittest.TestCase):
    def fixture(self, *, document=None, alter=None):
        plan,api,_,protected=fixture.ControllerSourceTests().fixture()
        document=ci_activation() if document is None else document
        raw=canonical_json(document)
        rows=tree(api,'8'*40,recursive=True)
        rows.extend([{'path':'site','type':'tree','mode':'040000','sha':'9'*40},
                     {'path':ACTIVATION_PATH,'type':'blob','mode':'100644','sha':api.add_blob(raw),'size':len(raw)}])
        if alter:alter(rows)
        api.add_tree('8'*40,rows)
        return plan,api,tuple(sorted((*protected,ACTIVATION_PATH))),rows,raw

    def invoke(self, plan, api, paths):
        return controller.authenticate_controller_activation(api,identity=plan['identity'],protected_paths=paths)

    def test_all_modes_bind_same_original_config_and_controller_bytes_without_execution(self):
        for mode in ACTIVATION_MODES:
            plan,api,paths,rows,raw=self.fixture(document=ci_activation(mode))
            with self.subTest(mode=mode):
                proof=self.invoke(plan,api,paths)
                self.assertEqual(proof.manifest.data,raw)
                self.assertEqual(proof.manifest.path,ACTIVATION_PATH)
                self.assertEqual(proof.sources.controller_sha,plan['identity']['controller_sha'])
                self.assertNotEqual(proof.sources.controller_sha,plan['identity']['tested_sha'])
                self.assertEqual(api.mutations,[])
                self.assertTrue(all(file.data.startswith(b'raise AssertionError') for file in proof.sources.files))

    def test_absent_manifest_policy_and_aliases_reject_before_source_admission(self):
        plan,api,paths,rows,raw=self.fixture()
        changes=((),list(paths),tuple(path for path in paths if path!=ACTIVATION_PATH),
                 tuple(sorted((*paths,'site/Mod-Base-Build-Activation.json'))),
                 tuple(sorted((*paths,'../invalid'))))
        with patch.object(controller,'authenticate_controller_sources') as read:
            for changed in changes:
                with self.subTest(paths=changed),self.assertRaises(MbError):self.invoke(plan,api,changed)
            read.assert_not_called()

    def test_missing_link_executable_alias_ancestor_and_size_metadata_reject(self):
        for kind in ('missing','link','executable','alias','parent','oversize','size'):
            def alter(rows):
                row=next(item for item in rows if item['path']==ACTIVATION_PATH)
                if kind=='missing':rows.remove(row)
                elif kind=='link':row['mode']='120000'
                elif kind=='executable':row['mode']='100755'
                elif kind=='alias':rows.append({'path':'Site','type':'tree','mode':'040000','sha':'9'*40})
                elif kind=='parent':rows[:]=[item for item in rows if item['path']!='site']
                elif kind=='oversize':row['size']=limits.MAX_CI_ACTIVATION_BYTES+1
                else:row['size']+=1
            plan,api,paths,rows,raw=self.fixture(alter=alter)
            with self.subTest(kind=kind),self.assertRaises(MbError):self.invoke(plan,api,paths)

    def test_foreign_repo_profile_and_invalid_mode_reject_even_with_valid_blob_identity(self):
        for changes in ({'repository':'foreign/mod'},{'profile':'quick-skin'},{'mode':'enabled'},{'jobs':{}}):
            plan,api,paths,rows,raw=self.fixture(document={**ci_activation(),**changes})
            with self.subTest(changes=changes),self.assertRaises(MbError):self.invoke(plan,api,paths)

    def test_second_manifest_read_drift_and_final_live_source_failure_cannot_return_evidence(self):
        plan,api,paths,rows,raw=self.fixture()
        original=controller._read_controller_activation
        count=0
        def changed(api,tree):
            nonlocal count
            count+=1
            if count==2:
                data=canonical_json({**ci_activation(),'mode':'shadow'})
                row=next(item for item in rows if item['path']==ACTIVATION_PATH)
                row.update(sha=api.add_blob(data),size=len(data));api.add_tree(tree,rows)
            return original(api,tree)
        with patch.object(controller,'_read_controller_activation',side_effect=changed),self.assertRaises(MbError):
            self.invoke(plan,api,paths)
        self.assertEqual(count,2)
        plan,api,paths,rows,raw=self.fixture()
        original_identity=controller.authenticate_source_identity
        count=0
        def stale(api,identity):
            nonlocal count
            count+=1
            if count==6:raise MbError('source changed at final bracket')
            return original_identity(api,identity)
        with patch.object(controller,'authenticate_source_identity',side_effect=stale),self.assertRaises(MbError):
            self.invoke(plan,api,paths)
        self.assertEqual(count,6)
