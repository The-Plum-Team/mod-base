"""Physical authored data with explicit Windows IO seams; no native/Linux boundary proof."""

import copy
import hashlib
import shutil
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import runtime_exports
from mod_base.build_ci.runtime_schema import bind_runtime_envelope
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_descriptor, ci_plan, ci_runtime_envelope


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        parent = Path(temporary)
        root = parent/'source'
        root.mkdir()
        envelope = ci_runtime_envelope()
        payloads = {'lanes/lane-a/result.json': b'{"authored":"opaque fixture"}\n',
                    'lanes/lane-a/runtime.log': b''}
        envelope['files'] = [{'path': name, 'lane_id': 'lane-a',
                              'role': 'runtime-log' if name.endswith('.log') else 'native-report',
                              'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(payloads.items())]
        for name, data in payloads.items():
            child = root/name
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(data)
        (root/grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(canonical_json(envelope))

        def read(root, name, *, max_bytes):
            try:
                data = (root/name).read_bytes()
            except OSError as error:
                raise MbError('fixture missing file') from error
            if len(data) > max_bytes:
                raise MbError('fixture byte cap')
            return data

        def records(root, **bounds):
            result = []
            for child in sorted(root.rglob('*')):
                if child.is_file():
                    data = child.read_bytes()
                    result.append({'path': child.relative_to(root).as_posix(), 'size': len(data),
                                   'sha256': hashlib.sha256(data).hexdigest()})
            if (len(result) > bounds['max_files'] or sum(file['size'] for file in result) > bounds['max_total_bytes']
                    or any(file['size'] > bounds['max_file_bytes'] for file in result)):
                raise MbError('fixture inventory caps')
            return result

        def copied(root, stage, **bounds):
            for child in root.rglob('*'):
                if child.is_file():
                    target = stage/child.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(child.read_bytes())
            return records(stage, **bounds)

        def atomic(output, writer):
            if output.exists():
                raise MbError('fixture output exists')
            stage = parent/'stage'
            stage.mkdir()
            try:
                result = writer(stage, stage)
                # Test-only publication copy avoids Windows scanner rename races.
                # Production still uses the unchanged MB1 exclusive atomic primitive.
                shutil.copytree(stage, output)
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

        stack.enter_context(patch.object(runtime_exports, 'validate_tree_entries'))
        stack.enter_context(patch.object(runtime_exports, 'read_child_file', side_effect=read))
        stack.enter_context(patch.object(runtime_exports, 'regular_data_records', side_effect=records))
        stack.enter_context(patch.object(runtime_exports, 'copy_regular_data_files', side_effect=copied))
        stack.enter_context(patch.object(runtime_exports, 'atomic_directory', side_effect=atomic))
        yield root, parent/'output', envelope


class RuntimeExportsTest(unittest.TestCase):
    def test_actual_inventory_and_copy_preserve_empty_log(self):
        with fixture() as (root, output, envelope):
            self.assertEqual(runtime_exports.verify_runtime_export(root, plan=ci_plan()), envelope)
            self.assertEqual(runtime_exports.materialize_runtime_export(root, output, plan=ci_plan()), envelope)
            for file in envelope['files']:
                self.assertEqual((root/file['path']).read_bytes(), (output/file['path']).read_bytes())
            self.assertEqual((output/'lanes/lane-a/runtime.log').stat().st_size, 0)

    def test_missing_extra_changed_bytes_and_sizes_reject(self):
        for change in ('missing', 'extra', 'same-size', 'size'):
            with self.subTest(change=change), fixture() as (root, output, envelope):
                report = root/envelope['files'][0]['path']
                if change == 'missing':
                    report.unlink()
                elif change == 'extra':
                    (root/'extra.log').write_bytes(b'')
                elif change == 'same-size':
                    report.write_bytes(b'x'*report.stat().st_size)
                else:
                    report.write_bytes(b'x')
                with self.assertRaises(MbError):
                    runtime_exports.materialize_runtime_export(root, output, plan=ci_plan())
                self.assertFalse(output.exists())

    def test_noncanonical_duplicate_unknown_and_plan_mismatch_before_payload_reads(self):
        for change in ('noncanonical', 'duplicate', 'unknown', 'plan'):
            with fixture() as (root, _, envelope):
                raw = canonical_json(envelope)
                if change == 'noncanonical':
                    raw = b' '+raw
                elif change == 'duplicate':
                    raw = raw.replace(b'{', b'{"kind":"duplicate",', 1)
                elif change == 'unknown':
                    envelope['approval'] = True
                    raw = canonical_json(envelope)
                else:
                    envelope['plan_sha256'] = 'f'*64
                    raw = canonical_json(envelope)
                (root/grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(raw)
                with patch.object(runtime_exports, 'regular_data_records') as inventory, self.assertRaises(MbError):
                    runtime_exports.verify_runtime_export(root, plan=ci_plan())
                inventory.assert_not_called()

    def test_entry_cap_precedes_content_reads_and_envelope_is_rechecked(self):
        with patch.object(runtime_exports, 'validate_tree_entries', side_effect=MbError('entry cap')), \
                patch.object(runtime_exports, 'read_child_file') as read, self.assertRaises(MbError):
            runtime_exports.verify_runtime_export(Path('hostile'), plan=ci_plan())
        read.assert_not_called()
        with fixture() as (root, _, envelope):
            with patch.object(runtime_exports, 'read_child_file', side_effect=[canonical_json(envelope), b'{}']), \
                    self.assertRaisesRegex(MbError, 'changed'):
                runtime_exports.verify_runtime_export(root, plan=ci_plan())

    def test_scope_bounds_include_only_outer_envelope_overhead(self):
        for scope, files, size in (('lane', limits.MAX_CI_RUNTIME_FILES, limits.MAX_CI_RUNTIME_BYTES),
                                    ('complete', limits.MAX_CI_RUNTIME_AGGREGATE_FILES, limits.MAX_CI_RUNTIME_AGGREGATE_BYTES)):
            with fixture() as (root, _, envelope):
                envelope.update(scope=scope, lane_id='lane-a' if scope == 'lane' else None)
                raw = canonical_json(envelope)
                (root/grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(raw)
                original = runtime_exports.regular_data_records.side_effect
                with patch.object(runtime_exports, 'regular_data_records', side_effect=original) as inventory:
                    runtime_exports.verify_runtime_export(root, plan=ci_plan())
                    self.assertEqual(inventory.call_args.kwargs['max_files'], files+1)
                    self.assertEqual(inventory.call_args.kwargs['max_total_bytes'], size+len(raw))

    def test_final_source_and_stage_tampering_never_publish(self):
        for target in ('source', 'stage'):
            with fixture() as (root, output, _):
                def tamper():
                    selected = root if target == 'source' else root.parent/'stage'
                    (selected/'lanes/lane-a/runtime.log').write_bytes(b'tampered')
                with self.assertRaises(MbError):
                    runtime_exports._materialize_runtime_export(root, output, plan=ci_plan(), before_publish=tamper)
                self.assertFalse(output.exists())
                self.assertFalse((root.parent/'stage').exists())

    def test_preexisting_output_is_preserved(self):
        with fixture() as (root, output, _):
            output.mkdir()
            (output/'keep').write_bytes(b'original')
            with self.assertRaises(MbError):
                runtime_exports.materialize_runtime_export(root, output, plan=ci_plan())
            self.assertEqual((output/'keep').read_bytes(), b'original')

    def test_descriptor_scope_attempt_producer_and_exact_owning_build(self):
        for scope in ('lane', 'complete'):
            envelope = ci_runtime_envelope()
            envelope.update(scope=scope, lane_id='lane-a' if scope == 'lane' else None)
            descriptor = ci_descriptor('runtime' if scope == 'lane' else 'results', gate='packaged',
                                       unit_id=envelope['lane_id'], artifact_id=102)
            self.assertIs(bind_runtime_envelope(envelope, descriptor=descriptor, owning_build=ci_descriptor(), plan=ci_plan()), envelope)
            for change in ('scope', 'producer', 'plan', 'owner'):
                changed = copy.deepcopy(descriptor)
                owner = ci_descriptor()
                if change == 'scope':
                    changed['artifact']['name'] = grammar.ci_artifact_name('build', 43, 2)
                elif change == 'producer':
                    changed['producer']['graph_sha256'] = 'f'*64
                elif change == 'plan':
                    changed['plan_sha256'] = 'f'*64
                else:
                    owner['artifact']['digest'] = 'sha256:'+'f'*64
                with self.assertRaises(MbError):
                    bind_runtime_envelope(envelope, descriptor=changed, owning_build=owner, plan=ci_plan())
