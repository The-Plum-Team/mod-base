"""Closed validator hooks select byte admission without weakening source/account checks."""

import hashlib
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from mod_base.build_ci import controller, inputs
from mod_base.build_ci.toolchain import ToolBytesProof, ToolTreeProof
from mod_base.build_ci.worker import WorkerResult
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_envelope, ci_plan
from tests import test_ci_controller as controller_fixture


class ByteControllerTests(unittest.TestCase):
    def exercise(self, *, hook='verify_build', unit_id=None, changed=False, failure=None,
                 tools=None, digest='sha256:'+'b'*64):
        fixture=controller_fixture.ControllerExecutionTests()
        plan,api,_,protected=controller_fixture.ControllerSourceTests().fixture()
        sources=controller.authenticate_controller_sources(api,identity=plan['identity'],protected_paths=protected)
        config=controller_fixture.ci_config_with(sources)
        proof=ToolBytesProof(ToolTreeProof(('/opt/hostedtoolcache/python',),'a'*64,1,2,100),'sha256:'+'b'*64)
        result=WorkerResult(0,b'bounded native fixture result',False)
        with ExitStack() as stack:
            stack.enter_context(patch.object(controller,'authenticate_worker_account',side_effect=[fixture.validator,fixture.candidate]))
            stack.enter_context(patch.object(controller,'authenticate_host_boundary'))
            reads=stack.enter_context(patch.object(controller,'_authenticate_controller_read_copy',side_effect=[config,{} if changed else config]))
            launch=stack.enter_context(patch.object(controller,'execute_byte_fenced_worker',return_value=result,side_effect=failure))
            legacy=stack.enter_context(patch.object(controller,'execute_tool_fenced_worker'))
            terminate=stack.enter_context(patch.object(controller,'terminate_worker'))
            try:
                observed=controller.execute_byte_fenced_controller_validator(boundary=fixture.boundary,
                    validator=fixture.validator,sources=sources,tools=proof if tools is None else tools,
                    expected_digest=digest,plan=plan,hook=hook,unit_id=unit_id,
                    python='/opt/hostedtoolcache/python/bin/python',java_home=None,run_id=42,run_attempt=2)
                self.assertEqual(observed,result)
                self.assertEqual(reads.call_count,2)
                kwargs=launch.call_args.kwargs
                self.assertEqual(kwargs['tools'],proof)
                self.assertEqual(kwargs['expected_digest'],digest)
                self.assertEqual(kwargs['command'][-2:],('--hook',hook))
                self.assertEqual(kwargs['timeout_seconds'],config['timeouts']['validator_seconds'])
                self.assertEqual(kwargs['values'],{} if hook=='verify_build' else
                    {'MB_TARGET_ID' if hook=='verify_target' else 'MB_LANE_ID':unit_id})
                return plan
            finally:
                legacy.assert_not_called()
                terminate.assert_called_once_with(fixture.validator)
                if digest is None or type(digest) is not str or digest!='sha256:'+'b'*64 or tools is not None:
                    launch.assert_not_called()
                    reads.assert_not_called()

    def test_build_target_and_runtime_hooks_use_byte_route_and_retained_source_checks(self):
        plan=self.exercise()
        self.exercise(hook='verify_target',unit_id=plan['targets'][0]['id'])
        self.exercise(hook='verify_runtime',unit_id=plan['lanes'][0]['id'])

    def test_missing_digest_and_metadata_receipt_cannot_fall_back_to_legacy_execution(self):
        for arguments in ({'digest':None},{'digest':object()},{'digest':'sha256:'+'f'*64},
                          {'tools':object()}, {'tools':ToolTreeProof(('/opt/python',),'a'*64,1,2,100)}):
            with self.subTest(arguments=arguments),self.assertRaises(MbError):self.exercise(**arguments)

    def test_source_drift_and_failed_byte_execution_prevent_success_and_lock_validator(self):
        for arguments in ({'changed':True},{'failure':MbError('changed tool bytes')}):
            with self.subTest(arguments=arguments),self.assertRaises(MbError):self.exercise(**arguments)

    def test_closed_hook_and_unit_selection_reject_arbitrary_dispatch(self):
        for hook,unit in (('unknown',None),('verify_build','unit'),('verify_target','foreign'),('verify_runtime','foreign')):
            with self.subTest(hook=hook),self.assertRaises(MbError):self.exercise(hook=hook,unit_id=unit)


class ByteFrozenInputsTests(unittest.TestCase):
    def exercise(self, *, target=False, changed=False, envelope=None, digest='sha256:'+'b'*64, failure=None):
        fixture=controller_fixture.ControllerExecutionTests()
        plan=ci_plan();envelope=ci_envelope() if envelope is None else envelope
        if target:envelope={**envelope,'scope':'target','target_id':plan['targets'][0]['id']}
        proof=ToolBytesProof(ToolTreeProof(('/opt/hostedtoolcache/python',),'a'*64,1,2,100),'sha256:'+'b'*64)
        identities=((1,20),(1,30));execution=WorkerResult(0,b'bounded fixture result',False)
        with patch.object(inputs,'authenticate_worker_account',side_effect=[fixture.validator,fixture.candidate]), \
                patch.object(inputs,'_read_inputs',side_effect=[identities,((1,99),(1,30)) if changed else identities]) as reads, \
                patch.object(inputs,'execute_byte_fenced_controller_validator',return_value=execution,side_effect=failure) as launch, \
                patch.object(inputs,'execute_controller_validator') as legacy, \
                patch.object(inputs,'terminate_worker') as terminate:
            try:
                execute=inputs.execute_byte_fenced_target_validator if target else inputs.execute_byte_fenced_build_validator
                arguments={'target_id':envelope['target_id']} if target else {}
                result=execute(boundary=fixture.boundary,validator=fixture.validator,sources=None,tools=proof,
                    expected_digest=digest,plan=plan,envelope=envelope,python='/opt/hostedtoolcache/python/bin/python',
                    java_home=None,run_id=42,run_attempt=2,**arguments)
                self.assertEqual(result,inputs.BuildValidationExecution(execution,hashlib.sha256(canonical_json(envelope)).hexdigest()))
                self.assertEqual(reads.call_count,2)
                self.assertEqual(launch.call_args.kwargs['expected_digest'],digest)
                self.assertEqual(launch.call_args.kwargs['tools'],proof)
                self.assertEqual((launch.call_args.kwargs['hook'],launch.call_args.kwargs['unit_id']),
                    ('verify_target',envelope['target_id']) if target else ('verify_build',None))
            finally:
                legacy.assert_not_called();terminate.assert_called_once_with(fixture.validator)
                if digest is None or envelope['producer']['run_id']!=42:
                    reads.assert_not_called();launch.assert_not_called()

    def test_complete_and_exact_target_inputs_retain_digest_with_byte_route(self):
        for target in (False,True):
            with self.subTest(target=target):self.exercise(target=target)

    def test_missing_digest_and_wrong_producer_reject_before_reading_inputs(self):
        envelope=ci_envelope();envelope['producer']['run_id']=43
        for arguments in ({'digest':None},{'envelope':envelope}):
            with self.subTest(arguments=arguments),self.assertRaises(MbError):self.exercise(**arguments)

    def test_replaced_inputs_and_failed_byte_execution_cannot_return_bound_success(self):
        for target in (False,True):
            for arguments in ({'changed':True},{'failure':MbError('tool changed')}):
                with self.subTest(target=target,arguments=arguments),self.assertRaises(MbError):
                    self.exercise(target=target,**arguments)
