"""Actual historical/full API gate admission with explicit Windows extraction/read seams."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests import test_ci_gate_transport as gates


def merged_gate_fixture(gate='build', *, raw=None):
    fixture = gates.gate_transport_fixture(gate, raw=raw)
    plan, api = fixture[:2]
    identity = plan['identity']
    merged, controller = 'b'*40, 'c'*40
    pr = api.get_json(f'/repos/{api.repository}/pulls/7')
    pr.update(state='closed', merged=True, merged_at='2026-10-08T10:00:00Z', merge_commit_sha=merged)
    pr['base']['sha'] = controller
    api.add_response(f'/repos/{api.repository}/pulls/7', pr)
    api.add_commit(merged, identity['tested_tree'], parents=[identity['base_sha']])
    api.set_branch('master', controller, 'd'*40)
    api.add_compare(identity['base_sha'], merged, {'status': 'ahead', 'ahead_by': 2, 'behind_by': 0})
    api.add_compare(merged, controller, {'status': 'ahead', 'ahead_by': 3, 'behind_by': 0})
    return fixture


class MergedGateTransportTests(unittest.TestCase):
    def call(self, fixture, parent, **overrides):
        arguments = dict(descriptor=fixture[3], plan=fixture[0], gate=fixture[2]['gate'],
                         workflow_path=fixture[3]['producer']['workflow_path'],
                         build_workflow_path='.github/workflows/build-gate.yml',
                         controller_sha='c'*40, merged_sha='b'*40, temporary_root=parent)
        arguments.update(overrides)
        return transport.download_merged_gate_receipt(fixture[1], **arguments)

    def seams(self, fixture):
        return gates.GateTransportTests().seams(fixture)

    def test_both_original_full_gates_keep_producer_identity_and_numeric_id(self):
        for gate in ('build', 'packaged'):
            fixture = merged_gate_fixture(gate)
            before = copy.deepcopy(fixture[0:1]+fixture[2:4])
            extract, read = self.seams(fixture)
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as directory, extract, read, \
                    patch.object(fixture[1], 'download', wraps=fixture[1].download) as download:
                result = self.call(fixture, Path(directory))
                self.assertEqual(result, fixture[2])
                self.assertEqual(result['identity']['controller_sha'], fixture[0]['identity']['controller_sha'])
                self.assertNotEqual(result['identity']['controller_sha'], 'c'*40)
                self.assertEqual(list(Path(directory).iterdir()), [])
                with self.assertRaises(MbError):
                    gates.GateTransportTests().call(fixture, Path(directory))
            download.assert_called_once_with('/repos/example/mod/actions/artifacts/200/zip',
                                             max_bytes=fixture[3]['artifact']['size'])
            self.assertEqual(fixture[0:1]+fixture[2:4], before)
            self.assertEqual(fixture[1].mutations, [])

    def test_independent_bindings_kind_and_workflow_reject_before_api(self):
        for arguments in ({'controller_sha': False}, {'merged_sha': None}, {'gate': 'reuse'},
                          {'workflow_path': '.github/workflows/foreign.yml'},
                          {'build_workflow_path': '.github/workflows/foreign.yml'}):
            fixture = merged_gate_fixture()
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], 'get_json') as reads, self.assertRaises(MbError):
                self.call(fixture, Path(directory), **arguments)
            reads.assert_not_called()
        for field, value in (('name', grammar.ci_artifact_name('reuse', 42, 2)), ('digest', 'invalid')):
            fixture = merged_gate_fixture()
            fixture[3]['artifact'][field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, \
                    patch.object(fixture[1], 'get_json') as reads, self.assertRaises(MbError):
                self.call(fixture, Path(directory))
            reads.assert_not_called()

    def test_wrong_historical_source_tree_or_current_controller_reject_before_download(self):
        for mutation in ('unmerged', 'draft', 'head', 'tree', 'controller'):
            fixture = merged_gate_fixture()
            api = fixture[1]
            pr = api.get_json('/repos/example/mod/pulls/7')
            if mutation == 'unmerged':
                pr['merged'] = False
            elif mutation == 'draft':
                pr['draft'] = True
            elif mutation == 'head':
                pr['head']['sha'] = 'f'*40
            elif mutation == 'tree':
                api.add_commit('b'*40, 'f'*40, parents=[fixture[0]['identity']['base_sha']])
            else:
                api.set_branch('master', 'e'*40, 'f'*40)
            api.add_response('/repos/example/mod/pulls/7', pr)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    patch.object(api, 'download') as download, self.assertRaises(MbError):
                self.call(fixture, Path(directory))
            download.assert_not_called()

    def test_latest_attempt_original_head_success_kit_and_full_graph_still_required(self):
        for gate in ('build', 'packaged'):
            for mutation in ('attempt', 'head', 'failed', 'kit', 'partial-graph'):
                fixture = merged_gate_fixture(gate)
                run_id = fixture[3]['producer']['run_id']
                run = fixture[8][run_id]
                if mutation == 'attempt':
                    run['run_attempt'] = 3
                elif mutation == 'head':
                    run['head_sha'] = 'c'*40  # Current controller cannot replace the original producer.
                elif mutation == 'failed':
                    run['conclusion'] = 'failure'
                elif mutation == 'kit':
                    run['referenced_workflows'][0]['sha'] = 'f'*40
                else:
                    fixture[1].add_jobs(run_id, 2, fixture[4][gate][:-1])
                fixture[1].add_run(run)
                with self.subTest(gate=gate, mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                        patch.object(fixture[1], 'download') as download, self.assertRaises(MbError):
                    self.call(fixture, Path(directory))
                download.assert_not_called()

    def test_numeric_record_metadata_and_all_source_availability_remain_required(self):
        for artifact_id in (200, 100, 101, 102):
            for mutation in ('expired', 'digest', 'owner'):
                fixture = merged_gate_fixture('packaged')
                record = fixture[7][artifact_id]
                if mutation == 'expired':
                    record['expired'] = True
                elif mutation == 'digest':
                    record['digest'] = 'sha256:'+'f'*64
                else:
                    record['workflow_run']['id'] = 999
                fixture[1].add_artifact(record, fixture[6] if artifact_id == 200 else b'unused')
                extract, read = self.seams(fixture)
                with self.subTest(artifact_id=artifact_id, mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                        extract, read, self.assertRaises(MbError):
                    self.call(fixture, Path(directory))

    def test_packaged_owning_build_has_independent_latest_attempt_and_enrollment(self):
        for mutation in ('attempt', 'enrollment'):
            fixture = merged_gate_fixture('packaged')
            if mutation == 'attempt':
                run = fixture[8][42]
                run['run_attempt'] = 3
                fixture[1].add_run(run)
            else:
                owner = fixture[2]['owning_build']['producer']
                owner.update(workflow_path='.github/workflows/foreign.yml',
                             workflow_ref='example/mod/.github/workflows/foreign.yml@refs/heads/master')
                fixture = merged_gate_fixture('packaged', raw=canonical_json(fixture[2]))
            extract, read = self.seams(fixture)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, \
                    extract, read, self.assertRaises(MbError):
                self.call(fixture, Path(directory))

    def test_actual_gate_timeline_and_record_canonical_bytes_cannot_be_bypassed(self):
        fixture = merged_gate_fixture()
        job = next(job for job in fixture[4]['build'] if job['steps'] and 'Verify complete' in job['name'])
        job['steps'][0]['started_at'] = '2026-10-07T10:01:00Z'
        fixture[1].add_jobs(42, 2, fixture[4]['build'])
        extract, read = self.seams(fixture)
        with tempfile.TemporaryDirectory() as directory, extract, read, self.assertRaises(MbError):
            self.call(fixture, Path(directory))
        for raw in (canonical_json(merged_gate_fixture()[2])+b'\n',
                    canonical_json({**merged_gate_fixture()[2], 'extra': True}),
                    canonical_json(merged_gate_fixture()[2]).replace(b'"mode":"full"', b'"mode":"full","mode":"full"')):
            fixture = merged_gate_fixture(raw=raw)
            extract, read = self.seams(fixture)
            with tempfile.TemporaryDirectory() as directory, extract, read:
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_record_digest_corruption_rejects_before_extraction(self):
        fixture = merged_gate_fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(fixture[1], 'download', return_value=b'wrong'), \
                patch.object(transport, 'extract') as extract, self.assertRaisesRegex(MbError, 'ZIP length or digest'):
            self.call(fixture, Path(directory))
        extract.assert_not_called()

    def test_historical_state_attempt_source_and_caller_movement_after_download_rejects(self):
        for mutation in ('merged_at', 'controller', 'attempt', 'source', 'plan', 'descriptor'):
            fixture = merged_gate_fixture()
            original = fixture[1].download
            def downloading(path, **kwargs):
                data = original(path, **kwargs)
                if mutation == 'merged_at':
                    pr = fixture[1].get_json('/repos/example/mod/pulls/7')
                    pr['merged_at'] = '2026-10-08T10:01:00Z'
                    fixture[1].add_response('/repos/example/mod/pulls/7', pr)
                elif mutation == 'controller':
                    fixture[1].set_branch('master', 'f'*40, 'e'*40)
                elif mutation == 'attempt':
                    run = fixture[8][42]
                    run['run_attempt'] = 3
                    fixture[1].add_run(run)
                elif mutation == 'source':
                    record = fixture[7][100]
                    record['expired'] = True
                    fixture[1].add_artifact(record, b'unused')
                elif mutation == 'plan':
                    fixture[0]['identity']['kit']['sha'] = 'f'*40
                else:
                    fixture[3]['artifact']['digest'] = 'sha256:'+'f'*64
                return data
            extract, read = self.seams(fixture)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, extract, read, \
                    patch.object(fixture[1], 'download', side_effect=downloading):
                with self.assertRaises(MbError):
                    self.call(fixture, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_api_and_private_extraction_errors_propagate_without_fallback_or_residue(self):
        fixture = merged_gate_fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(fixture[1], 'download', side_effect=MbError('API unavailable')), \
                self.assertRaisesRegex(MbError, 'API unavailable'):
            self.call(fixture, Path(directory))
        with tempfile.TemporaryDirectory() as directory, patch.object(transport, 'extract', side_effect=MbError('unsafe ZIP')):
            with self.assertRaisesRegex(MbError, 'unsafe ZIP'):
                self.call(fixture, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])
