"""Original historical newest Build admission uses actual fake API/graph/metadata logic."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.selection import (revalidate_latest_merged_pr_build, select_latest_merged_pr_build,
                                         select_latest_pr_build)
from mod_base.errors import MbError
from mod_base.model import grammar
from tests import test_ci_build_selection as selections


def merged_selection_fixture(*, same=False):
    fixture = selections.selection_fixture()
    plan, api = fixture[:2]
    identity = plan['identity']
    merged = identity['tested_sha'] if same else 'b' * 40
    controller = 'c' * 40
    pr = api.get_json('/repos/example/mod/pulls/7')
    pr.update(state='closed', merged=True, merged_at='2026-10-08T10:00:00Z', merge_commit_sha=merged)
    pr['base']['sha'] = controller
    api.add_response('/repos/example/mod/pulls/7', pr)
    if not same:
        api.add_commit(merged, identity['tested_tree'], parents=[identity['base_sha']])
    api.set_branch('master', controller, 'd' * 40)
    api.add_compare(identity['base_sha'], merged, {'status': 'ahead', 'ahead_by': 2, 'behind_by': 0})
    api.add_compare(merged, controller, {'status': 'ahead', 'ahead_by': 3, 'behind_by': 0})
    return fixture, controller, merged


class MergedBuildSelectionTests(unittest.TestCase):
    def call(self, fixture, controller, merged, **overrides):
        args = dict(plan=fixture[0], workflow_path='.github/workflows/build-gate.yml',
                    controller_sha=controller, merged_sha=merged)
        args.update(overrides)
        return select_latest_merged_pr_build(fixture[1], **args)

    def revalidate(self, fixture, controller, merged):
        return revalidate_latest_merged_pr_build(fixture[1], descriptor=fixture[2], plan=fixture[0],
            workflow_path='.github/workflows/build-gate.yml', controller_sha=controller, merged_sha=merged)

    def test_original_controller_attempt_and_descriptor_survive_final_merge(self):
        for same in (False, True):
            fixture, controller, merged = merged_selection_fixture(same=same)
            before = copy.deepcopy(fixture[0:1] + fixture[2:3])
            with self.subTest(same=same):
                result = self.call(fixture, controller, merged)
                self.assertEqual(result, fixture[2])
                self.assertNotEqual(result['producer']['api_head_sha'], controller)
                self.assertEqual(result['identity']['tested_sha'], fixture[0]['identity']['tested_sha'])
                self.revalidate(fixture, controller, merged)
                self.assertEqual(fixture[0:1] + fixture[2:3], before)
                self.assertEqual(fixture[1].mutations, [])
                with self.assertRaises(MbError):
                    select_latest_pr_build(fixture[1], plan=fixture[0], workflow_path='.github/workflows/build-gate.yml')

    def test_newest_failed_cancelled_or_pending_never_uses_old_success(self):
        for status, conclusion in (('completed', 'failure'), ('completed', 'cancelled'),
                                   ('completed', 'neutral'), ('queued', None), ('in_progress', None)):
            fixture, controller, merged = merged_selection_fixture()
            selections.later_run(fixture, status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion):
                if status == 'completed':
                    with self.assertRaisesRegex(MbError, 'newest exact Build'):
                        self.call(fixture, controller, merged)
                else:
                    self.assertIsNone(self.call(fixture, controller, merged))
                with self.assertRaises(MbError):
                    self.revalidate(fixture, controller, merged)

    def test_no_original_run_is_absence_not_reuse_authority(self):
        fixture, controller, merged = merged_selection_fixture()
        fixture[3]['display_title'] = fixture[3]['display_title'].replace('pr=7', 'pr=8')
        fixture[1].add_run(fixture[3])
        self.assertIsNone(self.call(fixture, controller, merged))
        with self.assertRaises(MbError):
            self.revalidate(fixture, controller, merged)

    def test_invalid_original_bindings_reject_before_api(self):
        for args in ({'controller_sha': True}, {'merged_sha': None}, {'workflow_path': 'arbitrary.py'}):
            fixture, controller, merged = merged_selection_fixture()
            with self.subTest(args=args), patch.object(fixture[1], 'get_json') as reads, self.assertRaises(MbError):
                self.call(fixture, controller, merged, **args)
            reads.assert_not_called()

    def test_unmerged_wrong_tree_parents_or_current_controller_reject(self):
        for change in ('unmerged', 'tree', 'parents', 'controller'):
            fixture, controller, merged = merged_selection_fixture()
            plan, api = fixture[:2]
            if change == 'unmerged':
                pr = api.get_json('/repos/example/mod/pulls/7')
                pr['merged'] = False
                api.add_response('/repos/example/mod/pulls/7', pr)
            elif change == 'tree':
                api.add_commit(merged, 'f' * 40, parents=[plan['identity']['base_sha']])
            elif change == 'parents':
                api.add_commit(plan['identity']['tested_sha'], plan['identity']['tested_tree'],
                               parents=list(reversed(plan['identity']['tested_parents'])))
            else:
                api.set_branch('master', 'e' * 40, 'f' * 40)
            with self.subTest(change=change), self.assertRaises(MbError):
                self.call(fixture, controller, merged)

    def test_newest_attempt_kit_graph_and_missing_bundle_reject(self):
        for change in ('attempt', 'kit', 'graph', 'bundle'):
            fixture, controller, merged = merged_selection_fixture()
            api = fixture[1]
            if change == 'attempt':
                api.during_listing('/repos/example/mod/actions/workflows/build-gate.yml/runs',
                                   lambda: api.add_run({**fixture[3], 'run_attempt': 3}))
            elif change == 'kit':
                fixture[3]['referenced_workflows'][0]['sha'] = 'f' * 40
                api.add_run(fixture[3])
            elif change == 'graph':
                api.add_jobs(42, 2, [{'name': 'Unexpected', 'status': 'completed', 'conclusion': 'success'}])
            else:
                fixture[4]['name'] = grammar.ci_artifact_name('build', 42, 1)
                api.add_artifact(fixture[4], b'unused')
            with self.subTest(change=change), self.assertRaises(MbError):
                self.call(fixture, controller, merged)

    def test_newer_run_and_source_movement_during_artifact_selection_reject(self):
        for change in ('run', 'source'):
            fixture, controller, merged = merged_selection_fixture()
            api = fixture[1]
            callback = (lambda: selections.later_run(fixture, status='queued', conclusion=None)) if change == 'run' else (
                lambda: api.set_branch('master', 'e' * 40, 'f' * 40))
            api.during_listing('/repos/example/mod/actions/runs/42/artifacts', callback)
            with self.subTest(change=change), self.assertRaises(MbError):
                self.call(fixture, controller, merged)

    def test_original_plan_drift_rejects_live_and_historical_success_pending_and_absence(self):
        for historical in (False, True):
            for state in ('success', 'pending', 'absence'):
                fixture, controller, merged = merged_selection_fixture() if historical else (
                    selections.selection_fixture(), None, None)
                if state == 'pending':
                    selections.later_run(fixture, status='queued', conclusion=None)
                elif state == 'absence':
                    fixture[3]['display_title'] = fixture[3]['display_title'].replace('pr=7', 'pr=8')
                    fixture[1].add_run(fixture[3])
                def mutate():
                    fixture[0]['identity']['inventory_sha256'] = 'f' * 64
                    fixture[0]['plan_sha256'] = plan_sha256(fixture[0])
                fixture[1].during_listing('/repos/example/mod/actions/workflows/build-gate.yml/runs', mutate)
                with self.subTest(historical=historical, state=state), self.assertRaisesRegex(MbError, 'plan changed'):
                    if historical:
                        self.call(fixture, controller, merged)
                    else:
                        select_latest_pr_build(fixture[1], plan=fixture[0], workflow_path='.github/workflows/build-gate.yml')

    def test_revalidation_rejects_original_descriptor_mutation(self):
        fixture, controller, merged = merged_selection_fixture()
        fixture[1].during_listing('/repos/example/mod/actions/workflows/build-gate.yml/runs',
            lambda: fixture[2]['artifact'].update(digest='sha256:' + 'f' * 64))
        with self.assertRaisesRegex(MbError, 'descriptor changed'):
            self.revalidate(fixture, controller, merged)

    def test_api_failure_is_not_absence_or_old_success(self):
        fixture, controller, merged = merged_selection_fixture()
        with patch.object(fixture[1], 'get_json', side_effect=MbError('API unavailable')), self.assertRaises(MbError):
            self.call(fixture, controller, merged)


if __name__ == '__main__':
    unittest.main()
