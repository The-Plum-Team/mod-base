"""Historical bundle bytes with real envelope/hash/copy logic and explicit Windows syscall seams."""

import copy
from contextlib import contextmanager
import hashlib
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import exports, transport
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests import test_ci_merged_gate_pair as pairs
from tests import test_ci_transport as payloads


def merged_build_fixture():
    fixture = pairs.paired_fixture()
    envelope = payloads.transport_fixture()[3]
    bundle = fixture['build']['artifacts'][0]
    for key in ('identity', 'profile', 'plan_sha256'):
        envelope[key] = copy.deepcopy(fixture['plan'][key])
    envelope['producer'] = {key: value for key, value in bundle['producer'].items() if key != 'upload_window'}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for file in envelope['files']:
            archive.writestr(file['path'], (file['path']+'\n').encode())
        archive.writestr(grammar.CI_ENVELOPE_NAME, canonical_json(envelope))
    data = stream.getvalue()
    selected = bundle['artifact']
    selected.update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    fixture['packaged']['owning_build'] = copy.deepcopy(bundle)
    record = {'id': 100, 'name': selected['name'], 'size_in_bytes': len(data), 'digest': selected['digest'],
              'created_at': selected['created_at'], 'expires_at': selected['expires_at'], 'expired': False,
              'workflow_run': {'id': 42, 'head_branch': 'master', 'head_sha': bundle['producer']['api_head_sha']}}
    fixture['api'].add_artifact(record, data)
    fixture.update(envelope=envelope, payload_record=record)
    fixture['data'][100] = data
    fixture['raw'][100] = canonical_json(envelope)
    pairs.publish(fixture, 'build')
    pairs.publish(fixture, 'packaged')
    return fixture


class MergedBuildTests(unittest.TestCase):
    def call(self, fixture, output):
        return transport.download_merged_build(fixture['api'], build_descriptor=fixture['build_descriptor'],
            packaged_descriptor=fixture['packaged_descriptor'], plan=fixture['plan'],
            build_workflow_path='.github/workflows/build-gate.yml',
            packaged_workflow_path=fixture['packaged_descriptor']['producer']['workflow_path'],
            controller_sha='c'*40, merged_sha='b'*40, output=output)

    @contextmanager
    def seams(self, fixture, *, after_extract=None, after_copy=None, after_download=None):
        state = {}
        def inventory(root, **kwargs):
            exclude = kwargs.get('exclude', ())
            return sorted([{'path': path.relative_to(root).as_posix(), 'size': path.stat().st_size,
                            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                           for path in root.rglob('*') if path.is_file()
                           and path.relative_to(root).as_posix() not in exclude], key=lambda record: record['path'])
        def extract(data, root):
            # Only newly authored fixture ZIP data. This seam proves no hostile archive traversal.
            root.mkdir()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    destination = root/name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
            if after_extract:
                after_extract(root)
        def atomic(output, writer):
            stage = output.parent/'private-stage'
            stage.mkdir()
            state['stage'] = stage
            try:
                result = writer(stage, 0)
                stage.rename(output)
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        def copy_files(root, fd, **kwargs):
            for source in root.rglob('*'):
                if source.is_file():
                    destination = state['stage']/source.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
            records = inventory(state['stage'])
            if after_copy:
                after_copy(state['stage'])
            return records
        def downloaded(artifact_id):
            if after_download:
                after_download(artifact_id, state.get('stage'))
        with pairs.MergedGatePairTests().seams(fixture, downloaded) as downloads, \
                patch.object(transport, 'extract_build', side_effect=extract), \
                patch.object(exports, 'validate_tree_entries'), \
                patch.object(exports, 'read_child_file', side_effect=lambda root, name, **kw: (root/name).read_bytes()), \
                patch.object(exports, 'file_records', side_effect=inventory), \
                patch.object(exports, 'copy_regular_files', side_effect=copy_files), \
                patch.object(exports, 'atomic_directory', side_effect=atomic):
            yield downloads

    def test_original_complete_payload_bytes_and_envelope_publish_after_paired_recheck(self):
        fixture = merged_build_fixture()
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture) as downloads:
            output = Path(directory)/'output'
            self.assertEqual(self.call(fixture, output), fixture['envelope'])
            for file in fixture['envelope']['files']:
                self.assertEqual(hashlib.sha256((output/file['path']).read_bytes()).hexdigest(), file['sha256'])
            self.assertEqual((output/grammar.CI_ENVELOPE_NAME).read_bytes(), canonical_json(fixture['envelope']))
            self.assertEqual([path.name for path in Path(directory).iterdir()], ['output'])
            self.assertEqual([call.args[0].split('/')[-2] for call in downloads.call_args_list],
                             ['201', '200', '201', '200', '100', '201', '200', '201', '200'])
        self.assertEqual(fixture['api'].mutations, [])

    def test_preexisting_output_or_nonpath_rejects_before_api(self):
        fixture = merged_build_fixture()
        with tempfile.TemporaryDirectory() as directory:
            for output in (Path(directory), False):
                with self.subTest(output=output), patch.object(fixture['api'], 'get_json') as reads, self.assertRaises(MbError):
                    self.call(fixture, output)
                reads.assert_not_called()

    def test_corrupt_payload_zip_rejects_before_extraction_and_output(self):
        fixture = merged_build_fixture()
        fixture['api'].add_artifact(fixture['payload_record'], b'wrong')
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture), \
                patch.object(transport, 'extract_build') as extract:
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            extract.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_and_staged_byte_tampering_reject_actual_hash_verification(self):
        for stage in (False, True):
            fixture = merged_build_fixture()
            def tamper(root):
                (root/fixture['envelope']['files'][0]['path']).write_bytes(b'tampered')
            arguments = {'after_copy' if stage else 'after_extract': tamper}
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory, self.seams(fixture, **arguments):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_either_full_seal_expiry_inside_copy_forbids_atomic_publication(self):
        for artifact_id in (201, 200):
            fixture = merged_build_fixture()
            def changed(stage):
                record = fixture['records'][artifact_id]
                record['expired'] = True
                fixture['api'].add_artifact(record, fixture['data'][artifact_id])
            with self.subTest(artifact_id=artifact_id), tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_copy=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_historical_or_caller_change_inside_copy_cannot_publish(self):
        for target in ('source', 'plan', 'descriptor'):
            fixture = merged_build_fixture()
            def changed(stage):
                if target == 'source':
                    pr = fixture['api'].get_json('/repos/example/mod/pulls/7')
                    pr['merged_at'] = '2026-10-08T10:01:00Z'
                    fixture['api'].add_response('/repos/example/mod/pulls/7', pr)
                elif target == 'plan':
                    fixture['plan']['identity']['kit']['sha'] = 'f'*40
                else:
                    fixture['packaged_descriptor']['artifact']['digest'] = 'sha256:'+'f'*64
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_copy=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_staged_bytes_changed_during_final_pair_read_are_reverified(self):
        fixture = merged_build_fixture()
        def changed(artifact_id, stage):
            if stage is not None:
                (stage/fixture['envelope']['files'][0]['path']).write_bytes(b'tampered')
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_download=changed):
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            self.assertEqual(list(Path(directory).iterdir()), [])
