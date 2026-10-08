"""Original runtime request mechanics with explicit filesystem/UID seams, not Linux proof."""

import copy
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import root_request, runtime_root_request as request
from mod_base.build_ci.installation import KitInstallation
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json, strict_loads
from tests import test_ci_root_request as build_fixture
from tests.helpers import ci_runtime_envelope


def fixture():
    invocation, sources, plan, build = build_fixture.fixture(empty=True)
    runtime = ci_runtime_envelope()
    runtime.update(identity=copy.deepcopy(plan['identity']), plan_sha256=plan['plan_sha256'],
                   scope='lane', lane_id='lane-a')
    runtime['owning_build'].update(identity=copy.deepcopy(plan['identity']), plan_sha256=plan['plan_sha256'])
    return invocation, dict(boundary=build_fixture.BOUNDARY, validator=build_fixture.VALIDATOR,
        sources=sources, plan=plan, build=build, runtime=runtime, lane_id='lane-a',
        run_id=43, run_attempt=2, execution_nonce='a'*64)


class RuntimeRootRequestTests(unittest.TestCase):
    identities = ((1, 13), (1, 14), (1, 15))

    def seams(self, stack):
        for name in ('authenticate_host_boundary', 'authenticate_privileged_host_boundary', '_accounts', '_layout',
                     'authenticate_tree_private_access', 'authenticate_tree_read_access'):
            stack.enter_context(patch.object(request, name))
        stack.enter_context(patch.object(request, '_inspect_sources', return_value=(1, 12)))
        stack.enter_context(patch.object(request, '_source_root_identity', return_value=(1, 12)))
        stack.enter_context(patch.object(request, '_inspect_inputs', return_value=self.identities))

    def publish(self, *, fault=None):
        invocation, args = fixture()
        captured = {}
        reads = 0
        original = copy.deepcopy(tuple(args[k] for k in ('plan', 'build', 'runtime')))
        def inspect(*values):
            nonlocal reads
            reads += 1
            self.assertEqual(values[2:], original)
            for actual, key in zip(values[2:], ('plan', 'build', 'runtime')):
                self.assertIsNot(actual, args[key])
            if reads == 2:
                if fault in ('plan', 'build', 'runtime'):
                    args[fault]['profile'] = 'quick-skin' if args[fault]['profile'] == 'block-pops' else 'block-pops'
                if fault == 'stage':
                    captured['raw'] += b' '
                if fault == 'bytes':
                    raise MbError('actual input bytes changed')
                if fault == 'inode':
                    return ((1, 13), (1, 14), (1, 99))
            return self.identities
        def atomic(path, fill):
            self.assertEqual(path, Path(str(request.RUNTIME_ROOT_REQUEST_ROOT)))
            fill(Path('/private-stage'), 31)
            captured['published'] = True
        def write(fd, name, raw):
            self.assertEqual((fd, name), (31, 'ci-runtime-root-request.json'))
            captured['raw'] = raw
        with ExitStack() as stack:
            self.seams(stack)
            stack.enter_context(patch.object(request, '_inspect_inputs', side_effect=inspect))
            stack.enter_context(patch.object(request.os, 'urandom', return_value=b'R'*32))
            stack.enter_context(patch.object(request.os, 'O_NOFOLLOW', 0, create=True))
            stack.enter_context(patch.object(request.os, 'open', return_value=7))
            chmod = stack.enter_context(patch.object(request.os, 'fchmod', create=True))
            stack.enter_context(patch.object(request.os, 'fsync'))
            close = stack.enter_context(patch.object(request.os, 'close'))
            stack.enter_context(patch.object(request, 'write_new', side_effect=write))
            stack.enter_context(patch.object(request, 'read_child_file', side_effect=lambda *a, **kw: captured['raw']))
            stack.enter_context(patch.object(request, 'atomic_directory', side_effect=atomic))
            nonce = request.record_runtime_root_freeze_request(invocation, **args)
            chmod.assert_called_once_with(7, 0o600)
            close.assert_called_once_with(7)
        self.assertTrue(captured['published'])
        return invocation, args, nonce, captured['raw']

    def read(self, invocation, args, nonce, raw, *, fault=None):
        kit = args['plan']['identity']['kit']
        installed = KitInstallation(kit['sha'], kit['version'], kit['tree_digest'], 3, 3, 1, 11)
        files = {f.path: f.data for f in (args['sources'].config, *args['sources'].files)}
        if fault == 'source-byte':
            target = next(f for f in args['sources'].files if f.data)
            files[target.path] += b'x'
        with ExitStack() as stack:
            self.seams(stack)
            stack.enter_context(patch.object(request, '_read', side_effect=[raw, raw+b' ' if fault == 'record' else raw]))
            stack.enter_context(patch.object(request, 'read_privileged_kit_installation',
                return_value=replace(installed, digest='sha256:'+'f'*64) if fault == 'kit' else installed))
            stack.enter_context(patch.object(root_request, 'read_child_file', side_effect=lambda root, path, **kw: files[path]))
            if fault == 'source-root':
                stack.enter_context(patch.object(request, '_inspect_sources', return_value=(1, 99)))
            if fault == 'inputs':
                stack.enter_context(patch.object(request, '_inspect_inputs',
                    side_effect=[self.identities, ((1, 13), (1, 99), (1, 15))]))
            return request.read_runtime_root_freeze_request(invocation, boundary=args['boundary'],
                validator=args['validator'], nonce=nonce)

    def test_fixed_private_writer_and_reader_preserve_original_three_inputs_and_empty_sources(self):
        invocation, args, nonce, raw = self.publish()
        document = strict_loads(raw, label='fixture', max_bytes=len(raw))
        self.assertEqual(nonce, '52'*32)
        self.assertEqual(document['execution_nonce'], 'a'*64)
        self.assertNotIn('data', document['sources']['files'][0])
        self.assertEqual(document['build']['producer']['run_id'], 42)
        self.assertEqual(document['run_id'], 43)
        context = self.read(invocation, args, nonce, raw)
        self.assertEqual(context.sources, args['sources'])
        self.assertTrue(any(f.data == b'' for f in context.sources.files))
        self.assertEqual((context.plan, context.build, context.runtime), tuple(args[k] for k in ('plan', 'build', 'runtime')))
        self.assertEqual((context.lane_id, context.run_id, context.run_attempt, context.execution_nonce), ('lane-a', 43, 2, 'a'*64))

    def test_writer_rejects_original_caller_each_input_byte_inode_and_staged_record_drift(self):
        for fault in ('plan', 'build', 'runtime', 'bytes', 'inode', 'stage'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.publish(fault=fault)

    def test_reader_rejects_nonce_noncanonical_record_kit_source_bytes_and_input_drift(self):
        invocation, args, nonce, raw = self.publish()
        for fault in ('record', 'kit', 'source-byte', 'source-root', 'inputs'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.read(invocation, args, nonce, raw, fault=fault)
        for n, data in (('f'*64, raw), (nonce, raw+b' '),
                        (nonce, raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1))):
            with self.subTest(nonce=n, data=data[:20]), self.assertRaises(MbError):
                self.read(invocation, args, n, data)

    def test_reader_binds_exact_host_validator_and_executing_invocation(self):
        invocation, args, nonce, raw = self.publish()
        document = strict_loads(raw, label='fixture', max_bytes=len(raw))
        for section, key, value in (('boundary', 'inode', 11), ('validator', 'uid', 2002)):
            altered = copy.deepcopy(document)
            altered[section][key] = value
            with self.subTest(section=section), self.assertRaises(MbError):
                self.read(invocation, args, nonce, canonical_json(altered))
        for actual in (object(), replace(invocation, implementation_sha_override='f'*40)):
            with self.subTest(invocation=actual), self.assertRaises(MbError):
                self.read(actual, args, nonce, raw)

    def test_role_denial_precedes_reads_publication_and_unadmitted_cleanup(self):
        invocation, args = fixture()
        for name, operation in (
            ('authenticate_host_boundary', lambda: request.record_runtime_root_freeze_request(invocation, **args)),
            ('authenticate_privileged_host_boundary', lambda: request.read_runtime_root_freeze_request(invocation,
                boundary=args['boundary'], validator=args['validator'], nonce='a'*64)),
            ('authenticate_privileged_host_boundary', lambda: request.freeze_root_requested_runtime_validation(invocation,
                boundary=args['boundary'], validator=args['validator'], nonce='a'*64))):
            with ExitStack() as stack:
                stack.enter_context(patch.object(request, name, side_effect=MbError('role')))
                reading = stack.enter_context(patch.object(request, '_read'))
                publication = stack.enter_context(patch.object(request, 'atomic_directory'))
                stopping = stack.enter_context(patch.object(request, 'terminate_worker'))
                with self.assertRaises(MbError):
                    operation()
                reading.assert_not_called()
                publication.assert_not_called()
                stopping.assert_not_called()

    def test_private_reader_uses_fixed_path_filename_origin_and_separate_cap(self):
        boundary = build_fixture.BOUNDARY
        with patch.object(request, 'authenticate_tree_private_access') as private, \
                patch.object(request, '_read_private_record', return_value=b'opaque') as reading:
            self.assertEqual(request._read(boundary), b'opaque')
        private.assert_called_once_with(Path(str(request.RUNTIME_ROOT_REQUEST_ROOT)),
            owner_uid=boundary.uid, owner_gid=boundary.gid, max_entries=1)
        reading.assert_called_once_with(Path(str(request.RUNTIME_ROOT_REQUEST_ROOT)),
            name=request.grammar.CI_RUNTIME_ROOT_REQUEST_NAME, owner_uid=boundary.uid,
            owner_gid=boundary.gid, max_bytes=request.limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES,
            label='runtime Root request')

    def test_os_failure_rejects_missing_request_without_freezing(self):
        invocation, args = fixture()
        with ExitStack() as stack:
            self.seams(stack)
            stack.enter_context(patch.object(request, '_read', side_effect=OSError('missing original')))
            freezing = stack.enter_context(patch.object(request, 'freeze_handed_off_runtime_validation'))
            stopping = stack.enter_context(patch.object(request, 'terminate_worker'))
            with self.assertRaises(MbError):
                request.freeze_root_requested_runtime_validation(invocation,
                    boundary=args['boundary'], validator=args['validator'], nonce='a'*64)
            freezing.assert_not_called()
            stopping.assert_called_once_with(args['validator'])

    def test_fixed_root_operation_retains_execution_nonce_rechecks_original_and_quiesces(self):
        invocation, args, nonce, raw = self.publish()
        original = self.read(invocation, args, nonce, raw)
        for fault in (None, 'nonce', 'caller', 'post-read', 'freeze'):
            context = copy.deepcopy(original)
            closing = replace(copy.deepcopy(context), execution_nonce='b'*64) if fault == 'nonce' else copy.deepcopy(context)
            def freeze(**kwargs):
                self.assertEqual(kwargs['nonce'], 'a'*64)
                self.assertEqual((kwargs['run_id'], kwargs['lane_id']), (43, 'lane-a'))
                if fault == 'caller':
                    context.runtime['files'][0]['sha256'] = 'f'*64
                if fault == 'freeze':
                    raise MbError('freeze')
                return {'fixture': 'receipt'}
            with self.subTest(fault=fault), ExitStack() as stack:
                self.seams(stack)
                stack.enter_context(patch.object(request, 'read_runtime_root_freeze_request',
                    side_effect=[context, MbError('post-read') if fault == 'post-read' else closing]))
                stack.enter_context(patch.object(request, 'freeze_handed_off_runtime_validation', side_effect=freeze))
                stopping = stack.enter_context(patch.object(request, 'terminate_worker'))
                if fault is None:
                    self.assertEqual(request.freeze_root_requested_runtime_validation(invocation,
                        boundary=args['boundary'], validator=args['validator'], nonce=nonce), {'fixture': 'receipt'})
                else:
                    with self.assertRaises(MbError):
                        request.freeze_root_requested_runtime_validation(invocation,
                            boundary=args['boundary'], validator=args['validator'], nonce=nonce)
                stopping.assert_called_once_with(args['validator'])
