"""Closed runtime Root context data and compatibility, not physical/native execution proof."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.runtime_root_request_schema import validate_runtime_root_request
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_root_request, ci_runtime_root_request


class RuntimeRootRequestSchemaTests(unittest.TestCase):
    def test_new_kind_retains_cross_run_build_and_existing_build_kind_is_unchanged(self):
        document = ci_runtime_root_request()
        self.assertEqual(document['build']['producer']['run_id'], 42)
        self.assertEqual(document['runtime']['producer']['run_id'], 43)
        self.assertEqual(load_document(canonical_json(document), kind=document['kind']), document)
        self.assertEqual(validate_root_request(ci_root_request()), ci_root_request())
        with self.assertRaises(MbError):
            validate_root_request(document)
        with self.assertRaises(MbError):
            validate_runtime_root_request(ci_root_request())

    def test_original_three_input_bindings_reject_mixed_scope_lane_attempt_owner_and_plan(self):
        for mutate in (lambda d: d.update(lane_id='lane-b'), lambda d: d.update(run_id=42),
                       lambda d: d.update(run_attempt=3),
                       lambda d: d['build'].update(scope='target', target_id='target-a'),
                       lambda d: d['runtime'].update(scope='complete', lane_id=None),
                       lambda d: d['runtime']['owning_build']['producer'].update(run_id=44),
                       lambda d: d['runtime'].update(plan_sha256='f'*64),
                       lambda d: d['build'].update(plan_sha256='f'*64)):
            document = ci_runtime_root_request()
            mutate(document)
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                validate_runtime_root_request(document)

    def test_closed_metadata_separation_and_exact_types_reject_before_any_authority(self):
        for mutate in (lambda d: d.update(nonce=d['execution_nonce']),
                       lambda d: d.update(program='unsafe'), lambda d: d.update(hook='verify_build'),
                       lambda d: d.update(path='/tmp/export'), lambda d: d.update(permissions={}),
                       lambda d: d.update(schema_version=2), lambda d: d.update(run_id=True),
                       lambda d: d['boundary'].update(device=True),
                       lambda d: d['boundary'].update(home='/tmp/runner'),
                       lambda d: d['validator'].update(uid=d['boundary']['uid']),
                       lambda d: d['validator'].update(gid=d['boundary']['gid']),
                       lambda d: d['validator'].update(uid=0),
                       lambda d: d['sources'].update(controller_sha='e'*40),
                       lambda d: d['sources']['config'].update(path='scripts/ci/other.json'),
                       lambda d: d['sources']['files'][0].update(path='.git/config'),
                       lambda d: d['sources']['files'][0].update(mode='120000'),
                       lambda d: d['sources']['files'].append(copy.deepcopy(d['sources']['files'][0]))):
            document = ci_runtime_root_request()
            mutate(document)
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                validate_runtime_root_request(document)

    def test_original_caps_and_strict_json_are_not_relaxed(self):
        document = ci_runtime_root_request()
        for cap in ('MAX_CI_PLAN_BYTES', 'MAX_CI_ENVELOPE_BYTES', 'MAX_CI_RUNTIME_ENVELOPE_BYTES',
                    'MAX_CI_RUNTIME_ROOT_REQUEST_BYTES', 'MAX_CI_CONFIG_BYTES', 'MAX_CI_ADAPTER_TREE_BYTES'):
            with self.subTest(cap=cap), patch.object(limits, cap, 1), self.assertRaises(MbError):
                validate_runtime_root_request(document)
        self.assertEqual(limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES,
                         limits.MAX_CI_ROOT_REQUEST_BYTES + limits.MAX_CI_RUNTIME_ENVELOPE_BYTES)
        raw = canonical_json(document)
        for changed in (raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1),
                        raw.replace(b'"run_attempt":2', b'"run_attempt":NaN', 1), b'\xff'):
            with self.subTest(raw=changed[:40]), self.assertRaises(MbError):
                load_document(changed, kind=document['kind'])
