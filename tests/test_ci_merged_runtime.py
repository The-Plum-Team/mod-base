"""Actual pair/API/runtime ZIP/byte logic; explicit Windows IO seams prove no Linux/native validity."""

import copy
import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import runtime_exports, transport
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.errors import MbError
from mod_base.io import bounded_zip
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_runtime_envelope
from tests import test_ci_merged_gate_pair as pairs


def merged_runtime_fixture():
    fixture = pairs.paired_fixture()
    aggregate = next(descriptor for descriptor in fixture['packaged']['artifacts']
                     if grammar.parse_ci_artifact_name(descriptor['artifact']['name']).kind == 'results')
    envelope = ci_runtime_envelope()
    for key in ('identity', 'profile', 'plan_sha256'):
        envelope[key] = copy.deepcopy(fixture['plan'][key])
    envelope['producer'] = {key: value for key, value in aggregate['producer'].items() if key != 'upload_window'}
    envelope['owning_build'] = copy.deepcopy(fixture['build']['artifacts'][0])
    contents = {'lanes/lane-a/result.json': b'{"opaque":"authored fixture"}\n', 'lanes/lane-a/runtime.log': b''}
    envelope['files'] = [{'path': name, 'lane_id': 'lane-a', 'role': 'runtime-log' if name.endswith('.log') else 'native-report',
                          'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in sorted(contents.items())]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr(grammar.CI_RUNTIME_ENVELOPE_NAME, canonical_json(envelope))
    data = stream.getvalue()
    selected = aggregate['artifact']
    selected.update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    record = {'id': selected['id'], 'name': selected['name'], 'size_in_bytes': len(data), 'digest': selected['digest'],
              'created_at': selected['created_at'], 'expires_at': selected['expires_at'], 'expired': False,
              'workflow_run': {'id': aggregate['producer']['run_id'], 'head_branch': 'master',
                               'head_sha': aggregate['producer']['api_head_sha']}}
    fixture['api'].add_artifact(record, data)
    fixture['data'][selected['id']] = data
    fixture['raw'][selected['id']] = canonical_json(envelope)
    fixture['records'][selected['id']] = record
    fixture.update(envelope=envelope, aggregate_id=selected['id'])
    pairs.publish(fixture, 'packaged')
    return fixture


class MergedRuntimeTest(unittest.TestCase):
    def call(self, fixture, output):
        return transport.download_merged_runtime(fixture['api'], build_descriptor=fixture['build_descriptor'],
            packaged_descriptor=fixture['packaged_descriptor'], plan=fixture['plan'],
            build_workflow_path='.github/workflows/build-gate.yml',
            packaged_workflow_path=fixture['packaged_descriptor']['producer']['workflow_path'],
            controller_sha='c'*40, merged_sha='b'*40, output=output)

    @contextmanager
    def seams(self, fixture, *, after_copy=None, after_download=None, after_extract=None):
        state = {}
        def atomic(destination, writer):
            stage = destination.parent/'fixture-stage'
            stage.mkdir()
            try:
                result = writer(stage, stage)
                shutil.copytree(stage, destination)  # Windows publication seam; no atomic claim.
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        def opening(stage, name):
            target = stage/name
            target.parent.mkdir(parents=True, exist_ok=True)
            return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        def records(root, **bounds):
            rows = sorted([{'path': child.relative_to(root).as_posix(), 'size': child.stat().st_size,
                            'sha256': hashlib.sha256(child.read_bytes()).hexdigest()}
                           for child in root.rglob('*') if child.is_file()], key=lambda row: row['path'])
            if (len(rows) > bounds['max_files'] or sum(row['size'] for row in rows) > bounds['max_total_bytes']
                    or any(row['size'] > bounds['max_file_bytes'] for row in rows)):
                raise MbError('fixture byte bounds')
            return rows
        def copying(root, stage, **bounds):
            state.update(stage=stage, source=root)
            for child in root.rglob('*'):
                if child.is_file():
                    target = stage/child.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(child.read_bytes())
            copied = records(stage, **bounds)
            if after_copy:
                after_copy(stage, root)
            return copied
        original_extract = transport.extract_runtime
        def extracting(data, root, *, scope):
            result = original_extract(data, root, scope=scope)
            if after_extract:
                after_extract(root)
            return result
        def downloaded(artifact_id):
            if after_download:
                after_download(artifact_id, state)
        with ExitStack() as stack:
            downloads = stack.enter_context(pairs.MergedGatePairTests().seams(fixture, downloaded))
            stack.enter_context(patch.object(bounded_zip.atomic, 'atomic_directory', side_effect=atomic))
            stack.enter_context(patch.object(bounded_zip.atomic, '_open_new_file', side_effect=opening))
            stack.enter_context(patch.object(bounded_zip.atomic, '_seal_new_file', side_effect=os.fsync))
            stack.enter_context(patch.object(transport, 'extract_runtime', side_effect=extracting))
            stack.enter_context(patch.object(runtime_exports, 'validate_tree_entries'))
            stack.enter_context(patch.object(runtime_exports, 'read_child_file', side_effect=lambda root, name, **kw: (root/name).read_bytes()))
            stack.enter_context(patch.object(runtime_exports, 'regular_data_records', side_effect=records))
            stack.enter_context(patch.object(runtime_exports, 'copy_regular_data_files', side_effect=copying))
            stack.enter_context(patch.object(runtime_exports, 'atomic_directory', side_effect=atomic))
            yield downloads

    def test_original_full_runtime_bytes_empty_log_and_pair_are_preserved(self):
        fixture = merged_runtime_fixture()
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture) as downloads:
            output = Path(directory)/'output'
            self.assertEqual(self.call(fixture, output), fixture['envelope'])
            for file in fixture['envelope']['files']:
                self.assertEqual(hashlib.sha256((output/file['path']).read_bytes()).hexdigest(), file['sha256'])
            self.assertEqual((output/'lanes/lane-a/runtime.log').stat().st_size, 0)
            self.assertEqual([call.args[0].split('/')[-2] for call in downloads.call_args_list],
                             ['201', '200', '201', '200', str(fixture['aggregate_id']), '201', '200', '201', '200'])
            self.assertEqual([child.name for child in Path(directory).iterdir()], ['output'])
        self.assertEqual(fixture['api'].mutations, [])

    def test_output_preflight_rejects_before_api(self):
        fixture = merged_runtime_fixture()
        with tempfile.TemporaryDirectory() as directory:
            for output in (False, Path(directory)):
                with patch.object(fixture['api'], 'get_json') as reads, self.assertRaises(MbError):
                    self.call(fixture, output)
                reads.assert_not_called()

    def test_zip_digest_rejects_before_parser(self):
        fixture = merged_runtime_fixture()
        fixture['api'].add_artifact(fixture['records'][fixture['aggregate_id']], b'wrong')
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture), patch.object(transport, 'extract_runtime') as parser:
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            parser.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_payload_source_stage_and_final_api_read_tampering_reject(self):
        for when in ('extract', 'copy', 'final'):
            fixture = merged_runtime_fixture()
            def tamper(root):
                (root/'lanes/lane-a/runtime.log').write_bytes(b'tampered')
            arguments = ({'after_extract': tamper} if when == 'extract' else
                         {'after_copy': lambda stage, root: tamper(stage)} if when == 'copy' else
                         {'after_download': lambda artifact_id, state: tamper(state['stage']) if 'stage' in state else None})
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, **arguments):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_valid_but_different_owner_producer_or_scope_rejects_original_selection(self):
        for target in ('owner', 'producer', 'scope'):
            fixture = merged_runtime_fixture()
            def changed(root):
                document = copy.deepcopy(fixture['envelope'])
                if target == 'owner':
                    document['owning_build']['artifact']['digest'] = 'sha256:'+'f'*64
                elif target == 'producer':
                    document['producer']['graph_sha256'] = 'f'*64
                else:
                    document.update(scope='lane', lane_id='lane-a')
                validate_runtime_envelope(document, plan=fixture['plan'])  # Independent shape is valid.
                (root/grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(canonical_json(document))
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_extract=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_self_consistent_source_replacement_after_binding_cannot_publish(self):
        fixture = merged_runtime_fixture()
        roots = []
        original = transport._authenticate_artifact
        def observe(api, descriptor, identity):
            original(api, descriptor, identity)
            if roots:
                root = roots.pop()
                document = copy.deepcopy(fixture['envelope'])
                data = b'self-consistent replacement'
                log = next(file for file in document['files'] if file['role'] == 'runtime-log')
                log.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                (root/log['path']).write_bytes(data)
                (root/grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(canonical_json(document))
                self.assertEqual(runtime_exports.verify_runtime_export(root, plan=fixture['plan']), document)
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_extract=roots.append), \
                patch.object(transport, '_authenticate_artifact', side_effect=observe):
            with self.assertRaisesRegex(MbError, 'original runtime bytes changed'):
                self.call(fixture, Path(directory)/'output')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_original_seal_runtime_or_lane_expiry_inside_copy_forbids_output(self):
        for artifact_id in (201, 200, 102, 101):
            fixture = merged_runtime_fixture()
            def changed(stage, root):
                record = (fixture['records'].get(artifact_id) or fixture['api'].get_json(f'/repos/example/mod/actions/artifacts/{artifact_id}'))
                record['expired'] = True
                fixture['api'].add_artifact(record, fixture['data'].get(artifact_id, b'fixture'))
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_copy=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_caller_or_attempt_movement_inside_copy_forbids_output(self):
        for target in ('source', 'plan', 'build', 'packaged', 'attempt'):
            fixture = merged_runtime_fixture()
            def changed(stage, root):
                if target == 'source':
                    pr = fixture['api'].get_json('/repos/example/mod/pulls/7')
                    pr['merged_at'] = '2026-10-08T10:01:00Z'
                    fixture['api'].add_response('/repos/example/mod/pulls/7', pr)
                elif target == 'plan':
                    fixture['plan']['identity']['kit']['sha'] = 'f'*40
                elif target == 'attempt':
                    run = fixture['api'].get_json('/repos/example/mod/actions/runs/43')
                    run['run_attempt'] = 3
                    fixture['api'].add_response('/repos/example/mod/actions/runs/43', run)
                else:
                    fixture[target+'_descriptor']['artifact']['digest'] = 'sha256:'+'f'*64
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_copy=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])
