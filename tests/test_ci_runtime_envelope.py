"""Closed runtime inventory data; these tests do not establish native/Linux byte provenance."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci import runtime_schema
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_plan, ci_runtime_envelope


class RuntimeEnvelopeTest(unittest.TestCase):
    def reject(self, document, **context):
        with self.assertRaises(MbError):
            runtime_schema.validate_runtime_envelope(document, **context)

    def test_complete_and_lane_roundtrip_keep_original_identity(self):
        for scope in ('complete', 'lane'):
            document = ci_runtime_envelope()
            document.update(scope=scope, lane_id='lane-a' if scope == 'lane' else None)
            self.assertIs(runtime_schema.validate_runtime_envelope(document, plan=ci_plan()), document)
            self.assertEqual(load_document(canonical_json(document), kind=document['kind'], plan=ci_plan()), document)

    def test_closed_records_exact_types_and_versions(self):
        for target, key, value in (('document', 'approval', True), ('document', 'schema_version', 2),
                                  ('document', 'schema_version', True), ('file', 'size', True),
                                  ('file', 'sha256', 'z'*64), ('file', 'role', 'production'),
                                  ('lane', 'program', 'candidate.py')):
            with self.subTest(target=target, key=key):
                document = ci_runtime_envelope()
                container = document if target == 'document' else document[target+'s'][0]
                container[key] = value
                self.reject(document)

    def test_original_build_and_producer_bindings(self):
        for mutate in (
                lambda d: d['owning_build']['identity'].update(tested_tree='f'*40),
                lambda d: d['owning_build'].update(plan_sha256='f'*64),
                lambda d: d['owning_build'].update(profile='quick-skin'),
                lambda d: d['producer'].update(workflow_ref='other/repo/.github/workflows/on-demand-e2e.yml@refs/heads/master'),
                lambda d: d['owning_build']['artifact'].update(name='mb-ci-results--42--a2')):
            document = ci_runtime_envelope()
            mutate(document)
            self.reject(document)

    def test_paths_order_alias_prefix_traversal_and_reserved_envelopes(self):
        for name in ('../escape', '/absolute', 'a//b', '.git/config', 'a/.GiT/config',
                     'ci-runtime-envelope.json', 'CI-RUNTIME-ENVELOPE.JSON/file', 'ci-envelope.json'):
            document = ci_runtime_envelope()
            document['files'][0]['path'] = name
            self.reject(document)
        for names in (('b', 'a'), ('a', 'a'), ('a', 'a/b'), ('Lane/a', 'lane/b')):
            document = ci_runtime_envelope()
            document['files'] = [{**document['files'][0], 'path': name} for name in names]
            self.reject(document)

    def test_files_keep_the_mods_own_names_and_nothing_the_sealed_tree_refuses(self):
        # The tree walks of a sealed runtime export admit exactly ``tree.EXPORT_PATHS``.
        for name in ('lanes/lane-a/screenshots/Title Screen - 1.20.1+build.5.png', 'lanes/lane-a/logs/latest.log',
                     'profiles/fabric-26.1.1--26.1.1--full/crash-reports/crash-2026-10-08_12.34.56-client.txt'):
            document = ci_runtime_envelope()
            document['files'][0]['path'] = name
            with self.subTest(name=name):
                runtime_schema.validate_runtime_envelope(document)
        for name in ('lanes/lane-a/.hidden.png', '.cache/result.json', 'lanes/lane-a/double  space.png',
                     'lanes/lane-a/trailing.', ' lanes/lane-a/a.png', 'lanes/lane-a/a.png ', 'lanes/lane-a/tab\tname.png',
                     'lanes/lane-a/café.png', 'lanes/lane-a/colon:name.png', 'lanes\\lane-a\\a.png', 'x' * 129,
                     '/'.join(['d'] * (limits.MAX_BUNDLE_PATH_DEPTH + 1)), '', None, 7, ['lanes/lane-a/a.png']):
            document = ci_runtime_envelope()
            document['files'][0]['path'] = name
            with self.subTest(name=name):
                self.reject(document)

    def test_scope_lane_membership_and_coverage(self):
        for key, value in (('lane_id', 'lane-a'), ('scope', 'unknown')):
            document = ci_runtime_envelope()
            document[key] = value
            self.reject(document)
        document = ci_runtime_envelope()
        document.update(scope='lane', lane_id=None)
        self.reject(document)
        for lane in (None, 'other'):
            document = ci_runtime_envelope()
            document.update(scope='lane', lane_id='lane-a')
            document['files'][0]['lane_id'] = lane
            self.reject(document)
        document = ci_runtime_envelope()
        document['files'][0]['lane_id'] = None
        self.reject(document)  # Aggregate-only files cannot replace every lane's evidence.
        document = ci_runtime_envelope()
        document['files'].append({**document['files'][0], 'path': 'summary.json', 'lane_id': None})
        runtime_schema.validate_runtime_envelope(document)

    def test_native_role_bounds_and_empty_log_data(self):
        for role, cap in (('native-report', limits.MAX_CI_REPORT_BYTES),
                          ('runtime-log', limits.MAX_CI_LOG_BYTES),
                          ('screenshot', limits.MAX_CI_PNG_BYTES),
                          ('crash-report', limits.MAX_CI_LOG_BYTES)):
            document = ci_runtime_envelope()
            document['files'][0].update(role=role, size=cap)
            runtime_schema.validate_runtime_envelope(document)
            document['files'][0]['size'] += 1
            self.reject(document)
            document['files'][0]['size'] = 0
            if role in ('runtime-log', 'crash-report'):
                runtime_schema.validate_runtime_envelope(document)
            else:
                self.reject(document)

    def test_aggregate_preserves_each_lane_limits(self):
        document = ci_runtime_envelope()
        document['files'] = [{**document['files'][0], 'path': f'files/{i:04d}'} for i in range(513)]
        self.reject(document)
        document['files'] = [{**document['files'][0], 'path': f'files/{i:04d}',
                              'role': 'screenshot', 'size': limits.MAX_CI_PNG_BYTES} for i in range(8)]
        runtime_schema.validate_runtime_envelope(document)  # Exactly 256 MiB in this lane.
        document['files'].append({**document['files'][0], 'path': 'files/0008', 'size': 1})
        self.reject(document)
        # Aggregate records have their own budget, independently of every lane budget.
        document = ci_runtime_envelope()
        document['files'].extend({**document['files'][0], 'path': f'summary/{i:04d}',
                                  'lane_id': None, 'role': 'screenshot',
                                  'size': limits.MAX_CI_PNG_BYTES} for i in range(16))
        self.reject(document)

    def test_count_preflight_before_nested_records_and_document_byte_cap(self):
        for scope, cap in (('lane', limits.MAX_CI_RUNTIME_FILES),
                           ('complete', limits.MAX_CI_RUNTIME_AGGREGATE_FILES)):
            document = ci_runtime_envelope()
            document.update(scope=scope, files=[None]*(cap+1))
            with patch.object(runtime_schema, '_ENVELOPE') as nested:
                self.reject(document)
                nested.assert_not_called()
        document = ci_runtime_envelope()
        with self.assertRaises(MbError):
            load_document(b' '*(limits.MAX_CI_RUNTIME_ENVELOPE_BYTES+1), kind=document['kind'])
        raw = canonical_json(document)
        for malformed in (raw.replace(b'"size":128', b'"size":NaN'),
                          raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')):
            self.assertNotEqual(raw, malformed)
            with self.assertRaises(MbError):
                load_document(malformed, kind=document['kind'])

    def test_independent_plan_contract_and_order(self):
        plan = ci_plan()
        document = ci_runtime_envelope()
        for key in ('native_contract_sha256', 'id'):
            changed = copy.deepcopy(document)
            changed['lanes'][0][key] = 'b'*64 if key == 'native_contract_sha256' else 'other'
            self.reject(changed, plan=plan)
        plan['lanes'].append({**plan['lanes'][0], 'id': 'lane-b'})
        plan['targets'][0]['outputs'].extend(
            {**output, 'path': output['path'].replace('lane-a', 'lane-b'), 'lane_id': 'lane-b'}
            for output in list(plan['targets'][0]['outputs']))
        from mod_base.build_ci.protocol import plan_sha256
        plan['plan_sha256'] = plan_sha256(plan)
        document['plan_sha256'] = document['owning_build']['plan_sha256'] = plan['plan_sha256']
        document['lanes'].append({'id': 'lane-b', 'native_contract_sha256': plan['lanes'][1]['native_contract_sha256']})
        document['files'].append({**document['files'][0], 'path': 'lanes/lane-b/result.json', 'lane_id': 'lane-b'})
        runtime_schema.validate_runtime_envelope(document, plan=plan)
        document['lanes'].reverse()
        self.reject(document, plan=plan)
