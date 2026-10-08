"""Coherent original gate pairs use actual API/reader logic with explicit filesystem seams."""

import copy
from contextlib import contextmanager
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_PACKAGED_CALL, CI_PACKAGED_JOBS
from tests import test_ci_merged_gate_transport as single


def publish(fixture, gate):
    descriptor = fixture[gate+'_descriptor']
    raw = canonical_json(fixture[gate])
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(grammar.CI_GATE_NAME, raw)
    data = stream.getvalue()
    selected = descriptor['artifact']
    selected.update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    record = {'id': selected['id'], 'name': selected['name'], 'size_in_bytes': selected['size'],
              'digest': selected['digest'], 'created_at': selected['created_at'],
              'expires_at': selected['expires_at'], 'expired': False,
              'workflow_run': {'id': descriptor['producer']['run_id'], 'head_branch': 'master',
                               'head_sha': descriptor['producer']['api_head_sha']}}
    fixture['api'].add_artifact(record, data)
    fixture['raw'][selected['id']] = raw
    fixture['data'][selected['id']] = data
    fixture['records'][selected['id']] = record


def paired_fixture():
    build = single.merged_gate_fixture('build')
    packaged = single.merged_gate_fixture('packaged')
    api = packaged[1]
    build[3]['artifact']['id'] = 201
    api.add_jobs(42, 2, build[4]['build'])
    job = next(job for job in packaged[4]['packaged']
               if job['name'] == f"{CI_PACKAGED_CALL} / {CI_PACKAGED_JOBS['gate']}")
    job.update(started_at='2026-10-07T10:06:00Z', completed_at='2026-10-07T10:10:00Z')
    job['steps'][0].update(started_at='2026-10-07T10:07:00Z', completed_at='2026-10-07T10:08:00Z')
    job['steps'][1].update(started_at='2026-10-07T10:09:00Z', completed_at='2026-10-07T10:10:00Z')
    api.add_jobs(43, 2, packaged[4]['packaged'])
    packaged[3]['producer']['upload_window'] = {'started_at': '2026-10-07T10:09:00Z',
                                               'completed_at': '2026-10-07T10:10:00Z'}
    packaged[3]['artifact']['created_at'] = '2026-10-07T10:09:30Z'
    fixture = {'api': api, 'plan': build[0], 'build': build[2], 'packaged': packaged[2],
               'build_descriptor': build[3], 'packaged_descriptor': packaged[3],
               'raw': {}, 'data': {}, 'records': {}, 'build_jobs': build[4]['build'],
               'runs': packaged[8]}
    publish(fixture, 'build')
    publish(fixture, 'packaged')
    return fixture


class MergedGatePairTests(unittest.TestCase):
    def call(self, fixture, parent, **overrides):
        arguments = {key: fixture[key] for key in ('build_descriptor', 'packaged_descriptor', 'plan')}
        arguments.update(build_workflow_path='.github/workflows/build-gate.yml',
                         packaged_workflow_path=fixture['packaged_descriptor']['producer']['workflow_path'],
                         controller_sha='c'*40, merged_sha='b'*40, temporary_root=parent)
        arguments.update(overrides)
        return transport.download_merged_gate_pair(fixture['api'], **arguments)

    @contextmanager
    def seams(self, fixture, after_download=None):
        state = {}
        original = fixture['api'].download
        def download(path, **kwargs):
            artifact_id = int(path.split('/')[-2])
            data = original(path, **kwargs)
            state['raw'] = fixture['raw'][artifact_id]
            if after_download is not None:
                after_download(artifact_id)
            return data
        with patch.object(fixture['api'], 'download', side_effect=download) as downloads, \
                patch.object(transport, 'extract', return_value=[grammar.CI_GATE_NAME]), \
                patch.object(transport, 'read_child_file', side_effect=lambda *args, **kwargs: state['raw']):
            yield downloads

    def test_coherent_pair_preserves_original_records_and_reads_each_seal_twice(self):
        fixture = paired_fixture()
        before = copy.deepcopy((fixture['plan'], fixture['build_descriptor'], fixture['packaged_descriptor']))
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture) as downloads:
            result = self.call(fixture, Path(directory))
            self.assertEqual(result, (fixture['build'], fixture['packaged']))
            self.assertEqual(result[0]['artifacts'][0], result[1]['owning_build'])
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual([call.args[0].split('/')[-2] for call in downloads.call_args_list],
                             ['201', '200', '201', '200'])
        self.assertEqual((fixture['plan'], fixture['build_descriptor'], fixture['packaged_descriptor']), before)
        self.assertEqual(fixture['api'].mutations, [])

    def test_malformed_or_mixed_pair_and_enrollment_reject_before_api(self):
        for mutation in ('controller', 'merged', 'workflow', 'unit', 'plan', 'same-id', 'same-run'):
            fixture = paired_fixture()
            arguments = {}
            if mutation == 'controller':
                arguments['controller_sha'] = False
            elif mutation == 'merged':
                arguments['merged_sha'] = None
            elif mutation == 'workflow':
                arguments['packaged_workflow_path'] = '.github/workflows/build-gate.yml'
            elif mutation == 'unit':
                fixture['packaged_descriptor']['artifact']['name'] = grammar.ci_artifact_name('tested', 43, 2, 'build')
            elif mutation == 'plan':
                fixture['packaged_descriptor']['plan_sha256'] = 'f'*64
            elif mutation == 'same-id':
                fixture['packaged_descriptor']['artifact']['id'] = 201
            else:
                fixture['packaged_descriptor']['producer']['run_id'] = 42
                fixture['packaged_descriptor']['artifact']['name'] = grammar.ci_artifact_name('tested', 42, 2, 'packaged')
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture['api'], 'get_json') as reads, self.assertRaises(MbError):
                self.call(fixture, Path(directory), **arguments)
            reads.assert_not_called()

    def test_individually_valid_packaged_gate_with_another_build_generation_is_rejected(self):
        fixture = paired_fixture()
        owner = fixture['packaged']['owning_build']
        owner['producer']['run_id'] = 44
        owner['artifact'].update(id=104, name=grammar.ci_artifact_name('build', 44, 2))
        run = copy.deepcopy(fixture['runs'][42])
        run['id'] = 44
        fixture['api'].add_run(run)
        fixture['api'].add_jobs(44, 2, fixture['build_jobs'])
        record = {'id': 104, 'name': owner['artifact']['name'], 'size_in_bytes': owner['artifact']['size'],
                  'digest': owner['artifact']['digest'], 'created_at': owner['artifact']['created_at'],
                  'expires_at': owner['artifact']['expires_at'], 'expired': False,
                  'workflow_run': {'id': 44, 'head_branch': 'master',
                                   'head_sha': fixture['plan']['identity']['controller_sha']}}
        fixture['api'].add_artifact(record, b'unused-native-payload')
        publish(fixture, 'packaged')
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture):
            actual = transport.download_merged_gate_receipt(fixture['api'], descriptor=fixture['packaged_descriptor'],
                plan=fixture['plan'], gate='packaged', workflow_path=fixture['packaged_descriptor']['producer']['workflow_path'],
                build_workflow_path='.github/workflows/build-gate.yml', controller_sha='c'*40, merged_sha='b'*40,
                temporary_root=Path(directory))
            self.assertEqual(actual['owning_build']['producer']['run_id'], 44)
            with self.assertRaisesRegex(MbError, 'different complete Build bundle'):
                self.call(fixture, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_earlier_build_seal_expiry_during_packaged_read_is_caught_by_second_pass(self):
        fixture = paired_fixture()
        def after(artifact_id):
            if artifact_id == 200:
                record = fixture['records'][201]
                record['expired'] = True
                fixture['api'].add_artifact(record, fixture['data'][201])
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after), self.assertRaises(MbError):
            self.call(fixture, Path(directory))

    def test_later_packaged_seal_expiry_during_second_build_read_refuses(self):
        fixture = paired_fixture()
        seen = 0
        def after(artifact_id):
            nonlocal seen
            if artifact_id == 201:
                seen += 1
                if seen == 2:
                    record = fixture['records'][200]
                    record['expired'] = True
                    fixture['api'].add_artifact(record, fixture['data'][200])
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after), self.assertRaises(MbError):
            self.call(fixture, Path(directory))

    def test_historical_generation_change_between_individual_readers_rejects(self):
        fixture = paired_fixture()
        original = transport.download_merged_gate_receipt
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs['gate'] == 'build':
                pr = fixture['api'].get_json('/repos/example/mod/pulls/7')
                pr['merged_at'] = '2026-10-08T10:01:00Z'
                fixture['api'].add_response('/repos/example/mod/pulls/7', pr)
            return result
        # Explicit timing seam after the actual individual reader, not a fake success result.
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture), \
                patch.object(transport, 'download_merged_gate_receipt', side_effect=read), \
                self.assertRaisesRegex(MbError, 'historical source changed'):
            self.call(fixture, Path(directory))

    def test_original_caller_plan_or_either_descriptor_substitution_rejects(self):
        for target in ('plan', 'build_descriptor', 'packaged_descriptor'):
            fixture = paired_fixture()
            def after(artifact_id):
                if target == 'plan':
                    fixture[target]['identity']['kit']['sha'] = 'f'*40
                else:
                    fixture[target]['artifact']['digest'] = 'sha256:'+'f'*64
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory, self.seams(fixture, after), \
                    self.assertRaises(MbError):
                self.call(fixture, Path(directory))

    def test_api_failure_in_later_gate_never_returns_an_earlier_partial_success(self):
        fixture = paired_fixture()
        failure = MbError('later gate unavailable')
        def after(artifact_id):
            if artifact_id == 200:
                raise failure
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after), self.assertRaises(MbError) as caught:
            self.call(fixture, Path(directory))
        self.assertIs(caught.exception, failure)
