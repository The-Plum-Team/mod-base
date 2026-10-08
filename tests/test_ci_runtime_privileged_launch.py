"""Explicit runtime process/parent contract; syscall/admission seams establish no hosted authority."""

import copy
import io
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import privileged_launch as launch, root_request, runtime_root_request, host, worker
from mod_base.build_ci.validation import validate_validation_receipt
from mod_base.errors import MbError
from tests import test_ci_privileged_launch as old_launch
from tests import test_ci_privileged_bootstrap as guard_fixture
from tests import test_ci_runtime_root_request as request_fixture
from tests.helpers import ci_validation


bootstrap = guard_fixture.bootstrap


class RuntimeProcessTests(unittest.TestCase):
    def arguments(self):
        return ['--operation', launch.grammar.CI_RUNTIME_FREEZE_OPERATION, *guard_fixture.entry_arguments()]

    def test_explicit_versioned_route_preserves_build_parser_and_rejects_unknown_selectors_before_loading(self):
        valid = self.arguments()
        self.assertEqual(bootstrap.RUNTIME_OPERATION, launch.grammar.CI_RUNTIME_FREEZE_OPERATION)
        self.assertEqual(bootstrap.RUNTIME_ENTRY_FLAGS, launch.PRIVILEGED_RUNTIME_FREEZE_FLAGS)
        self.assertNotIn('--operation', bootstrap._entry_arguments(guard_fixture.entry_arguments()))
        self.assertEqual(bootstrap._entry_arguments(valid)['--operation'], 'runtime-validation-v1')
        cases = [valid[:-1], valid + ['--operation', bootstrap.RUNTIME_OPERATION], valid[2:] + valid[:2], tuple(valid)]
        for value in ('verify_runtime', 'runtime-validation-v2', 'build', '', '../candidate', True):
            changed = valid.copy()
            changed[1] = value
            cases.append(changed)
        changed = valid.copy()
        changed[0] = '--command'
        cases.append(changed)
        for arguments in cases:
            with self.subTest(arguments=arguments), patch.object(bootstrap, 'load_fixed_kit') as loading, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(bootstrap.main(arguments), 2)
            loading.assert_not_called()

    def test_actual_runtime_process_rejects_unsupported_host_without_kit_import(self):
        if sys.platform == 'linux':
            arguments = ['--operation', 'runtime-validation-v2', *guard_fixture.entry_arguments()]
        else:
            arguments = self.arguments()
        result = subprocess.run((sys.executable, '-I', '-B', '-S', str(guard_fixture.PROGRAM), *arguments),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr.replace(b'\r\n', b'\n'), b'mod-base: private bootstrap rejected\n')

    def test_byte_admission_precedes_fixed_runtime_dispatch_and_never_calls_build(self):
        invocation, args = request_fixture.fixture()
        for failure in (None, 'composition', 'freeze'):
            events = []
            with self.subTest(failure=failure), ExitStack() as stack:
                stack.enter_context(patch.object(bootstrap, 'load_fixed_kit', side_effect=lambda **kw: events.append('load')))
                stack.enter_context(patch.object(host, 'authenticate_privileged_host_boundary', side_effect=lambda b: events.append('host')))
                stack.enter_context(patch.object(worker, 'authenticate_worker_account', return_value=args['validator']))
                def compose(**kwargs):
                    events.append('compose')
                    if failure == 'composition':
                        raise MbError('composition')
                    return invocation
                stack.enter_context(patch.object(root_request, 'build_root_freeze_invocation', side_effect=compose))
                build = stack.enter_context(patch.object(root_request, 'freeze_root_requested_build_validation'))
                runtime = stack.enter_context(patch.object(runtime_root_request, 'freeze_root_requested_runtime_validation',
                    side_effect=MbError('freeze') if failure == 'freeze' else None))
                stopping = stack.enter_context(patch.object(worker, 'terminate_worker'))
                stack.enter_context(redirect_stderr(io.StringIO()))
                self.assertEqual(bootstrap.main(self.arguments()), 0 if failure is None else 2)
                self.assertEqual(events, ['load', 'host', 'compose'])
                build.assert_not_called()
                if failure == 'composition':
                    runtime.assert_not_called()
                    stopping.assert_called_once_with(args['validator'])
                else:
                    self.assertEqual(runtime.call_args.args, (invocation,))
                    self.assertEqual(runtime.call_args.kwargs['nonce'], 'd'*64)
                    stopping.assert_not_called()  # Real fixed runtime sealing owns cleanup.

    def test_runtime_import_failure_precedes_account_lookup(self):
        with patch.object(bootstrap, 'load_fixed_kit'), \
                patch.dict(sys.modules, {'mod_base.build_ci.runtime_root_request': None}), \
                patch.object(worker, 'authenticate_worker_account') as account, \
                patch.object(worker, 'terminate_worker') as stopping, redirect_stderr(io.StringIO()):
            self.assertNotEqual(bootstrap.main(self.arguments()), 0)
        account.assert_not_called()
        stopping.assert_not_called()


class RuntimeParentLaunchTests(unittest.TestCase):
    def fixture(self):
        _, args, _ = old_launch.PrivilegedLaunchTests().fixture()
        invocation, runtime = request_fixture.fixture()
        invocation = replace(invocation, repo_root=Path('/home/runner/controller'))
        context = runtime_root_request.RuntimeRootFreezeContext(runtime['sources'], runtime['plan'], runtime['build'],
            runtime['runtime'], runtime['lane_id'], runtime['run_id'], runtime['run_attempt'], runtime['execution_nonce'])
        return invocation, args, context

    def seams(self, stack, invocation, args, context):
        mocks = old_launch.PrivilegedLaunchTests().seams(stack, invocation, args, context)
        mocks['read_runtime_root_freeze_request'] = stack.enter_context(patch.object(launch,
            'read_runtime_root_freeze_request', return_value=context))
        receipt = ci_validation('verify_runtime', context.lane_id)
        receipt.update(identity=context.plan['identity'], plan_sha256=context.plan['plan_sha256'],
            profile=context.plan['profile'], run_id=context.run_id, run_attempt=context.run_attempt,
            source_config_sha256=context.sources.config.sha256,
            input_sha256=launch._runtime_context(context.plan, context.build, context.runtime,
                lane_id=context.lane_id, run_id=context.run_id, run_attempt=context.run_attempt)[0])
        mocks['verify_validation_export'].return_value = receipt
        def verify(root, **kwargs):
            value = mocks['verify_validation_export'].return_value
            validate_validation_receipt(value, plan=kwargs['plan'])
            for key in ('hook', 'unit_id', 'run_id', 'run_attempt', 'source_config_sha256', 'input_sha256'):
                if value[key] != kwargs[key]:
                    raise MbError('original receipt context differs')
            return copy.deepcopy(value)
        mocks['verify_validation_export'].side_effect = verify
        return mocks

    def test_fixed_runtime_command_and_original_cross_run_context_use_independent_receipt_reads(self):
        invocation, args, context = self.fixture()
        with ExitStack() as stack:
            mocks = self.seams(stack, invocation, args, context)
            receipt = launch.execute_privileged_runtime_freeze_request(invocation, **args)
            self.assertEqual(receipt['run_id'], 43)
            self.assertEqual(context.build['producer']['run_id'], 42)
            command = mocks['_control'].call_args.args[0]
            self.assertEqual(command[:4], (args['python'], '-I', '-B', '-S'))
            self.assertEqual(command[4], str(launch.PRIVILEGED_BOOTSTRAP_ROOT / launch.grammar.CI_BOOTSTRAP_PROGRAM_NAME))
            values = bootstrap._entry_arguments(list(command[5:]))
            self.assertEqual(values['--operation'], 'runtime-validation-v1')
            self.assertEqual(values['--nonce'], args['nonce'])
            self.assertEqual(mocks['_control'].call_args.kwargs,
                dict(timeout=20.0, accepted=frozenset({0}), cwd=Path(str(launch.PRIVILEGED_KIT_ROOT))))
            self.assertEqual(mocks['verify_validation_export'].call_count, 2)
            self.assertEqual(mocks['authenticate_privileged_bootstrap'].call_count, 4)
            self.assertEqual(mocks['authenticate_toolchain_bytes'].call_count, 4)
            self.assertEqual(mocks['read_runtime_root_freeze_request'].call_count, 3)
            mocks['read_root_freeze_request'].assert_not_called()
            mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_role_and_missing_admission_forbid_process_and_unadmitted_cleanup(self):
        invocation, args, context = self.fixture()
        for name in ('authenticate_privileged_host_boundary', 'authenticate_privileged_bootstrap',
                     'authenticate_toolchain_bytes', '_execution_tool_paths', 'build_root_freeze_invocation',
                     'read_runtime_root_freeze_request'):
            with self.subTest(name=name), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                mocks[name].side_effect = MbError('admission')
                with self.assertRaises(MbError):
                    launch.execute_privileged_runtime_freeze_request(invocation, **args)
                mocks['_control'].assert_not_called()
                mocks['verify_validation_export'].assert_not_called()
                if name == 'authenticate_privileged_host_boundary':
                    mocks['authenticate_worker_account'].assert_not_called()
                    mocks['terminate_worker'].assert_not_called()
                else:
                    mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_context_drift_after_process_or_during_receipt_cannot_replace_original(self):
        for point in ('process', 'receipt'):
            for key in ('plan', 'build', 'runtime'):
                invocation, args, context = self.fixture()
                with self.subTest(point=point, key=key), ExitStack() as stack:
                    mocks = self.seams(stack, invocation, args, context)
                    original = mocks['verify_validation_export'].side_effect
                    def mutate():
                        getattr(context, key)['profile'] = 'quick-skin' if context.plan['profile'] == 'block-pops' else 'block-pops'
                    if point == 'process':
                        mocks['_control'].side_effect = lambda *a, **kw: (mutate(), b'')[1]
                    else:
                        def read(*a, **kw):
                            value = original(*a, **kw)
                            mutate()
                            return value
                        mocks['verify_validation_export'].side_effect = read
                    with self.assertRaises(MbError):
                        launch.execute_privileged_runtime_freeze_request(invocation, **args)
                    mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_late_original_request_program_tools_receipt_and_root_changes_reject_return(self):
        for fault in ('request', 'program', 'tools', 'receipt', 'root'):
            invocation, args, context = self.fixture()
            with self.subTest(fault=fault), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                if fault == 'request':
                    mocks['read_runtime_root_freeze_request'].side_effect = [context, context, replace(context, execution_nonce='f'*64)]
                elif fault == 'program':
                    mocks['authenticate_privileged_bootstrap'].side_effect = [None, None, None, MbError('late program')]
                elif fault == 'tools':
                    mocks['authenticate_toolchain_bytes'].side_effect = [args['tools']]*3 + [MbError('late tools')]
                elif fault == 'receipt':
                    value = copy.deepcopy(mocks['verify_validation_export'].return_value)
                    other = copy.deepcopy(value)
                    other['reports'][0]['sha256'] = 'f'*64
                    mocks['verify_validation_export'].side_effect = [value, other]
                else:
                    mocks['_sealed_identity'].side_effect = [(1, 13), (1, 99)]
                with self.assertRaises(MbError):
                    launch.execute_privileged_runtime_freeze_request(invocation, **args)
                mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_failed_non_silent_or_os_process_never_reads_receipt(self):
        invocation, args, context = self.fixture()
        for result in (MbError('process'), OSError('process'), b'forged stdout'):
            with self.subTest(result=result), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                if isinstance(result, Exception):
                    mocks['_control'].side_effect = result
                else:
                    mocks['_control'].return_value = result
                with self.assertRaises(MbError):
                    launch.execute_privileged_runtime_freeze_request(invocation, **args)
                mocks['verify_validation_export'].assert_not_called()
                mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_installed_sdk_source_admission_brackets_all_four_tool_checks_and_rejects_drift(self):
        for fault in (None, 'source', 'path', 'late'):
            invocation, args, context = self.fixture()
            args['python'] = old_launch.InstalledPythonLaunchTests().installed()
            executable = '/opt/hostedtoolcache/Python/3.11.17/x64/bin/python3.11'
            with self.subTest(fault=fault), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                source = stack.enter_context(patch.object(launch, 'authenticate_privileged_python_installation', return_value=executable))
                if fault == 'source': source.side_effect = MbError('source')
                elif fault == 'path': source.return_value = '/wrong/python'
                elif fault == 'late': source.side_effect = [executable]*3 + [MbError('late source')]
                if fault is None:
                    launch.execute_installed_python_runtime_freeze_request(invocation, **args)
                    self.assertEqual(source.call_count, 4)
                    self.assertEqual(mocks['_control'].call_args.args[0][0], executable)
                else:
                    with self.assertRaises(MbError):
                        launch.execute_installed_python_runtime_freeze_request(invocation, **args)
                    if fault != 'late': mocks['_control'].assert_not_called()
                mocks['terminate_worker'].assert_called_once_with(old_launch.VALIDATOR)

    def test_unsupported_sdk_or_wrong_context_type_never_launches(self):
        invocation, args, context = self.fixture()
        for proof in (object(), replace(old_launch.InstalledPythonLaunchTests().installed(), version='../escape')):
            with self.subTest(proof=proof), ExitStack() as stack:
                mocks = self.seams(stack, invocation, args, context)
                args['python'] = proof
                with self.assertRaises(MbError):
                    launch.execute_installed_python_runtime_freeze_request(invocation, **args)
                mocks['authenticate_worker_account'].assert_not_called()
        invocation, args, context = self.fixture()
        with ExitStack() as stack:
            mocks = self.seams(stack, invocation, args, context)
            mocks['read_runtime_root_freeze_request'].return_value = object()
            with self.assertRaises(MbError):
                launch.execute_privileged_runtime_freeze_request(invocation, **args)
            mocks['_control'].assert_not_called()
