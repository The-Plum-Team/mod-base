"""Combined original physical bytes with actual API/ZIP/readers and explicit Windows IO seams."""

import copy
import hashlib
import io
import shutil
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import exports, transport
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests import test_ci_merged_build as builds
from tests import test_ci_merged_gate_pair as pairs
from tests import test_ci_merged_runtime as runtimes


def merged_inputs_fixture():
    fixture = runtimes.merged_runtime_fixture()
    build = builds.merged_build_fixture()
    fixture['build']['artifacts'] = copy.deepcopy(build['build']['artifacts'])
    fixture['build_envelope'] = copy.deepcopy(build['envelope'])
    fixture['packaged']['owning_build'] = copy.deepcopy(fixture['build']['artifacts'][0])
    fixture['envelope']['owning_build'] = copy.deepcopy(fixture['build']['artifacts'][0])
    for key in ('data', 'raw'):
        fixture[key][100] = build[key][100]
    fixture['records'][100] = build['payload_record']
    fixture['api'].add_artifact(build['payload_record'], build['data'][100])
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fixture['data'][102])) as original, \
            zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in original.namelist():
            archive.writestr(name, canonical_json(fixture['envelope']) if name == grammar.CI_RUNTIME_ENVELOPE_NAME else original.read(name))
    data = stream.getvalue()
    fixture['data'][102] = data
    fixture['raw'][102] = canonical_json(fixture['envelope'])
    descriptor = next(descriptor for descriptor in fixture['packaged']['artifacts'] if descriptor['artifact']['id'] == 102)
    descriptor['artifact'].update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    fixture['records'][102].update(size_in_bytes=len(data), digest=descriptor['artifact']['digest'])
    fixture['api'].add_artifact(fixture['records'][102], data)
    pairs.publish(fixture, 'build')
    pairs.publish(fixture, 'packaged')
    return fixture


class MergedInputsTest(unittest.TestCase):
    def call(self, fixture, output):
        return transport.download_merged_inputs(fixture['api'], build_descriptor=fixture['build_descriptor'],
            packaged_descriptor=fixture['packaged_descriptor'], plan=fixture['plan'],
            build_workflow_path='.github/workflows/build-gate.yml',
            packaged_workflow_path=fixture['packaged_descriptor']['producer']['workflow_path'],
            controller_sha='c'*40, merged_sha='b'*40, output=output)

    @contextmanager
    def seams(self, fixture, *, after_build_copy=None, after_runtime_copy=None, after_download=None):
        def atomic(destination, writer):
            stage = destination.parent/'combined-fixture-stage'
            stage.mkdir()
            try:
                result = writer(stage, stage)
                shutil.copytree(stage, destination)  # Windows publication seam; no atomic/UID proof.
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        def inventory(root, **bounds):
            excluded = bounds.get('exclude', ())
            return sorted([{'path': child.relative_to(root).as_posix(), 'size': child.stat().st_size,
                            'sha256': hashlib.sha256(child.read_bytes()).hexdigest()}
                           for child in root.rglob('*') if child.is_file() and child.relative_to(root).as_posix() not in excluded],
                          key=lambda row: row['path'])
        def copying(root, stage, **bounds):
            for child in root.rglob('*'):
                if child.is_file():
                    target = stage/child.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(child.read_bytes())
            rows = inventory(stage)
            if after_build_copy:
                after_build_copy(stage)
            return rows
        with runtimes.MergedRuntimeTest().seams(fixture, after_copy=after_runtime_copy, after_download=after_download) as downloads, \
                patch.object(transport, 'atomic_directory', side_effect=atomic), \
                patch.object(transport, 'validate_tree_entries'), \
                patch.object(exports, 'atomic_directory', side_effect=atomic), \
                patch.object(exports, 'validate_tree_entries'), \
                patch.object(exports, 'read_child_file', side_effect=lambda root, name, **kw: (root/name).read_bytes()), \
                patch.object(exports, 'file_records', side_effect=inventory), \
                patch.object(exports, 'copy_regular_files', side_effect=copying):
            yield downloads

    def test_both_original_complete_byte_sets_publish_under_fixed_children(self):
        fixture = merged_inputs_fixture()
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture) as downloads:
            output = Path(directory)/'output'
            self.assertEqual(self.call(fixture, output), (fixture['build_envelope'], fixture['envelope']))
            self.assertEqual(sorted(child.name for child in output.iterdir()), ['build', 'runtime'])
            for child, envelope in (('build', fixture['build_envelope']), ('runtime', fixture['envelope'])):
                for file in envelope['files']:
                    self.assertEqual(hashlib.sha256((output/child/file['path']).read_bytes()).hexdigest(), file['sha256'])
            ids = [call.args[0].split('/')[-2] for call in downloads.call_args_list]
            self.assertEqual(len(ids), 26)
            self.assertEqual(ids.count('100'), 1)
            self.assertEqual(ids.count('102'), 1)
            self.assertEqual([child.name for child in Path(directory).iterdir()], ['output'])
        self.assertEqual(fixture['api'].mutations, [])

    def test_bad_output_and_bindings_reject_before_api(self):
        fixture = merged_inputs_fixture()
        with tempfile.TemporaryDirectory() as directory:
            for output in (False, Path(directory)):
                with patch.object(fixture['api'], 'get_json') as reads, self.assertRaises(MbError):
                    self.call(fixture, output)
                reads.assert_not_called()
            fixture['packaged_descriptor']['plan_sha256'] = 'f'*64
            with patch.object(fixture['api'], 'get_json') as reads, self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            reads.assert_not_called()

    def test_runtime_failure_after_build_copy_publishes_neither_input(self):
        fixture = merged_inputs_fixture()
        fixture['api'].add_artifact(fixture['records'][102], b'wrong')
        observed = []
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_build_copy=lambda stage: observed.append(stage)):
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            self.assertTrue(observed)  # Build bytes really reached the private stage first.
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_build_bytes_changed_during_runtime_copy_reject_at_combined_admission(self):
        fixture = merged_inputs_fixture()
        def tamper(stage, root):
            (stage.parent/'build'/fixture['build_envelope']['files'][0]['path']).write_bytes(b'tampered')
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_runtime_copy=tamper):
            with self.assertRaises(MbError):
                self.call(fixture, Path(directory)/'output')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_original_caller_movement_is_caught_even_when_inner_snapshots_stay_valid(self):
        for target in ('plan', 'build', 'packaged'):
            fixture = merged_inputs_fixture()
            def changed(stage):
                if target == 'plan':
                    fixture['plan']['identity']['kit']['sha'] = 'f'*40
                else:
                    fixture[target+'_descriptor']['artifact']['digest'] = 'sha256:'+'f'*64
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_build_copy=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_final_pair_read_cannot_hide_either_payload_or_extra_child_tampering(self):
        for target in ('build', 'runtime', 'extra', 'expired'):
            fixture = merged_inputs_fixture()
            observed = []
            def changed(artifact_id, state):
                if 'stage' not in state:
                    return
                parent = state['stage'].parent
                if not (parent/'runtime').exists():
                    return  # Wait until both inner publications completed.
                observed.append(artifact_id)
                if target == 'extra':
                    (parent/'extra').write_bytes(b'')
                elif target == 'expired':
                    record = fixture['records'][102]
                    record['expired'] = True
                    fixture['api'].add_artifact(record, fixture['data'][102])
                else:
                    envelope = fixture['build_envelope'] if target == 'build' else fixture['envelope']
                    (parent/target/envelope['files'][0]['path']).write_bytes(b'tampered')
            with tempfile.TemporaryDirectory() as directory, self.seams(fixture, after_download=changed):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory)/'output')
                self.assertTrue(observed)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_complete_entry_cap_precedes_final_payload_content_reads(self):
        fixture = merged_inputs_fixture()
        with tempfile.TemporaryDirectory() as directory, self.seams(fixture), \
                patch.object(transport, 'validate_tree_entries', side_effect=MbError('combined entry cap')) as entries:
            with self.assertRaisesRegex(MbError, 'combined entry cap'):
                self.call(fixture, Path(directory)/'output')
            self.assertEqual(entries.call_args.kwargs['max_entries'], limits.MAX_CI_ORIGINAL_INPUT_ENTRIES)
            self.assertEqual(list(Path(directory).iterdir()), [])
