"""Pre-plan readiness reads cannot promote draft observations into execution evidence."""

import copy
import dataclasses
import unittest
from unittest.mock import patch

from mod_base.build_ci.authenticate import authenticate_pr_identity, read_pr_generation, read_pr_on_base
from mod_base.errors import MbError
from tests.helpers import ci_plan
from tests.test_ci_protocol import seeded_pr


class PrGenerationTests(unittest.TestCase):
    def seed(self, *, draft=False, merge=True):
        plan, api, pr = seeded_pr()
        pr["draft"] = draft
        if not merge:
            pr["merge_commit_sha"] = None
        path = f"/repos/{api.repository}/pulls/7"
        api.add_response(path, pr)
        return api, pr, path, plan["identity"]["controller_sha"]

    def test_ready_and_draft_reads_need_no_merge_object_or_worker(self):
        for draft in (False, True):
            for merge in (False, True):
                api, pr, path, controller = self.seed(draft=draft, merge=merge)
                with patch.object(api, "get_json", wraps=api.get_json) as reads:
                    observed = read_pr_generation(api, pr_number=7, controller_sha=controller)
                with self.subTest(draft=draft, merge=merge):
                    self.assertEqual(observed.draft, draft)
                    self.assertEqual(observed.merge_sha, pr["merge_commit_sha"])
                    self.assertEqual(observed.base_sha, controller)
                    self.assertEqual(observed.head_sha, pr["head"]["sha"])
                    self.assertEqual(observed.base_branch, "master")
                    self.assertEqual(observed.controller_tree, "8" * 40)
                    self.assertEqual(observed.repository, api.repository)
                    self.assertEqual(observed.pr_number, 7)
                    self.assertEqual(observed.head_branch, pr["head"]["ref"])
                    # One observation: the repository, its default head and the pull request, once each.
                    self.assertEqual([call.args[0] for call in reads.call_args_list],
                                     [f"/repos/{api.repository}", f"/repos/{api.repository}/branches/master", path])
                    self.assertEqual(api.mutations, [])
                    with self.assertRaises(dataclasses.FrozenInstanceError):
                        observed.draft = not draft

    def test_malformed_inputs_reject_before_any_api_read(self):
        api, _, _, controller = self.seed()
        for number, sha in ((True, controller), (0, controller), (-1, controller),
                            ("7", controller), (7, "bad"), (7, None)):
            with patch.object(api, "get_json") as reads, self.subTest(number=number, sha=sha):
                with self.assertRaises(MbError):
                    read_pr_generation(api, pr_number=number, controller_sha=sha)
                reads.assert_not_called()

    def test_closed_or_foreign_or_malformed_generation_refuses(self):
        changes = [lambda p: p.update(number=True), lambda p: p.update(number=8),
                   lambda p: p.update(state="closed"), lambda p: p.update(draft=1),
                   lambda p: p.pop("draft"), lambda p: p.pop("merge_commit_sha"),
                   lambda p: p.update(merge_commit_sha="bad"), lambda p: p.update(head=None),
                   lambda p: p["head"].update(repo=None),
                   lambda p: p["head"]["repo"].update(full_name="fork/mod"),
                   lambda p: p["base"]["repo"].update(full_name="foreign/mod"),
                   lambda p: p["base"].update(ref="other"), lambda p: p["base"].update(sha="f" * 40),
                   lambda p: p["head"].update(sha=True), lambda p: p["head"].update(ref="../unsafe")]
        for index, change in enumerate(changes):
            api, pr, path, controller = self.seed(draft=True, merge=False)
            change(pr)
            api.add_response(path, pr)
            with self.subTest(index=index), self.assertRaises(MbError):
                read_pr_generation(api, pr_number=7, controller_sha=controller)

    def test_a_later_observation_shows_every_readiness_and_source_change(self):
        changes = [lambda p: p.update(draft=False), lambda p: p["head"].update(sha="e" * 40),
                   lambda p: p["head"].update(ref="renamed"), lambda p: p.update(merge_commit_sha="e" * 40)]
        for index, change in enumerate(changes):
            api, pr, path, controller = self.seed(draft=True)
            first = read_pr_generation(api, pr_number=7, controller_sha=controller)
            self.assertEqual(read_pr_generation(api, pr_number=7, controller_sha=controller), first)
            change(pr)
            api.add_response(path, pr)
            with self.subTest(index=index):
                self.assertNotEqual(read_pr_generation(api, pr_number=7, controller_sha=controller), first)
        for change in (lambda p: p.update(state="closed"), lambda p: p["base"].update(sha="e" * 40)):
            api, pr, path, controller = self.seed(draft=True)
            read_pr_generation(api, pr_number=7, controller_sha=controller)
            change(pr)
            api.add_response(path, pr)
            with self.assertRaises(MbError):
                read_pr_generation(api, pr_number=7, controller_sha=controller)

    def test_stale_controller_and_moved_default_head_refuse(self):
        api, _, _, controller = self.seed()
        with self.assertRaises(MbError):
            read_pr_generation(api, pr_number=7, controller_sha="e" * 40)
        api, _, _, controller = self.seed()
        read_pr_generation(api, pr_number=7, controller_sha=controller)
        api.set_branch("master", "e" * 40, "e" * 40)
        with self.assertRaisesRegex(MbError, "controller has moved"):
            read_pr_generation(api, pr_number=7, controller_sha=controller)

    def test_api_error_is_not_absence_or_deferral(self):
        for target in ("", "/branches/master", "/pulls/7"):
            api, _, _, controller = self.seed(draft=True)
            original = api.get_json
            failure = MbError("unavailable API")

            def read(endpoint, **kwargs):
                if endpoint == f"/repos/{api.repository}{target}":
                    raise failure
                return original(endpoint, **kwargs)

            with patch.object(api, "get_json", side_effect=read), self.subTest(target=target):
                with self.assertRaises(MbError) as caught:
                    read_pr_generation(api, pr_number=7, controller_sha=controller)
                self.assertIs(caught.exception, failure)

    def test_retained_generation_does_not_alias_returned_api_dict(self):
        api, pr, path, controller = self.seed()
        original = api.get_json
        shared = copy.deepcopy(pr)

        def read(endpoint, **kwargs):
            return shared if endpoint == path else original(endpoint, **kwargs)

        with patch.object(api, "get_json", side_effect=read):
            observed = read_pr_generation(api, pr_number=7, controller_sha=controller)
        shared["head"].clear()
        shared["draft"] = True
        self.assertEqual(observed.head_sha, pr["head"]["sha"])
        self.assertFalse(observed.draft)

    def test_renamed_default_branch_refuses_whatever_its_head(self):
        for head in ("e" * 40, None):
            api, _, _, controller = self.seed()
            api.set_branch("renamed", head or controller, "8" * 40)
            api.add_response(f"/repos/{api.repository}", {"full_name": api.repository, "default_branch": "renamed"})
            with self.subTest(head=head), self.assertRaises(MbError):
                read_pr_generation(api, pr_number=7, controller_sha=controller)

    def test_observation_does_not_replace_full_ready_merge_admission(self):
        for draft, merge in ((True, True), (True, False), (False, False)):
            api, _, _, controller = self.seed(draft=draft, merge=merge)
            read_pr_generation(api, pr_number=7, controller_sha=controller)
            with self.subTest(draft=draft, merge=merge), self.assertRaises(MbError):
                authenticate_pr_identity(api, ci_plan()["identity"])

    def test_a_second_merge_admission_refuses_every_change_since_the_first(self):
        changes = [lambda a, p, path: p.update(draft=True),
                   lambda a, p, path: p["head"].update(sha="e" * 40),
                   lambda a, p, path: p.update(merge_commit_sha="e" * 40),
                   lambda a, p, path: p.update(state="closed"),
                   lambda a, p, path: p["base"].update(sha="e" * 40),
                   lambda a, p, path: a.set_branch("master", "e" * 40, "e" * 40),
                   lambda a, p, path: a.add_response(f"/repos/{a.repository}",
                       {"full_name": a.repository, "default_branch": "renamed"})]
        for index, change in enumerate(changes):
            api, pr, path, _ = self.seed()
            authenticate_pr_identity(api, ci_plan()["identity"])
            change(api, pr, path)
            api.add_response(path, pr)
            with self.subTest(index=index), self.assertRaises(MbError):
                authenticate_pr_identity(api, ci_plan()["identity"])


class PrOnBaseTests(unittest.TestCase):
    """The pull request alone, against a base its caller holds: one request, the same observation."""

    TREE = "8" * 40
    seed = PrGenerationTests.seed

    def read(self, api, controller, **changes):
        return read_pr_on_base(api, **{"pr_number": 7, "base_branch": "master", "controller_sha": controller,
                                       "controller_tree": self.TREE, **changes})

    def test_one_request_gives_the_observation_of_the_full_read(self):
        for draft in (False, True):
            for merge in (False, True):
                api, _, path, controller = self.seed(draft=draft, merge=merge)
                whole = read_pr_generation(api, pr_number=7, controller_sha=controller)
                self.assertEqual(whole.controller_tree, self.TREE)
                api, pr, path, controller = self.seed(draft=draft, merge=merge)
                with patch.object(api, "get_json", wraps=api.get_json) as reads:
                    alone = self.read(api, controller)
                with self.subTest(draft=draft, merge=merge):
                    self.assertEqual(alone, whole)
                    self.assertEqual((alone.draft, alone.merge_sha), (draft, pr["merge_commit_sha"]))
                    self.assertEqual([call.args[0] for call in reads.call_args_list], [path])
                    self.assertEqual(api.mutations, [])

    def test_the_base_is_bound_to_what_the_caller_holds(self):
        api, pr, _, controller = self.seed()
        # The tree is the caller's word for its controller commit: the pull request names no tree.
        self.assertEqual(self.read(api, controller, controller_tree="7" * 40).controller_tree, "7" * 40)
        for changes in ({"base_branch": "release"}, {"controller_sha": "e" * 40}, {"pr_number": 8}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.read(api, controller, **changes)
        self.assertNotEqual(pr["base"]["sha"], "e" * 40)

    def test_the_default_branch_and_its_head_are_left_to_the_caller(self):
        # What `read_pr_generation` observes besides the pull request is not observed here.
        api, _, _, controller = self.seed()
        first = self.read(api, controller)
        api.set_branch("master", "e" * 40, "e" * 40)
        api.add_response(f"/repos/{api.repository}", {"full_name": api.repository, "default_branch": "renamed"})
        self.assertEqual(self.read(api, controller), first)
        with self.assertRaises(MbError):
            read_pr_generation(api, pr_number=7, controller_sha=controller)

    def test_malformed_inputs_reject_before_any_api_read(self):
        api, _, _, controller = self.seed()
        cases = [{"pr_number": number} for number in (True, 0, -1, "7", None)]
        cases += [{"base_branch": branch} for branch in ("", "a b", "../unsafe", None, b"master")]
        cases += [{"controller_sha": sha} for sha in ("bad", None, "E" * 40, controller[:39])]
        cases += [{"controller_tree": tree} for tree in ("bad", None, "E" * 40, self.TREE[:39])]
        for changes in cases:
            with patch.object(api, "get_json") as reads, self.subTest(changes=changes):
                with self.assertRaises(MbError):
                    self.read(api, controller, **changes)
                reads.assert_not_called()

    def test_closed_or_foreign_or_malformed_generation_refuses(self):
        changes = [lambda p: p.update(number=8), lambda p: p.update(state="closed"), lambda p: p.pop("draft"),
                   lambda p: p.pop("merge_commit_sha"), lambda p: p.update(merge_commit_sha="bad"),
                   lambda p: p["head"]["repo"].update(full_name="fork/mod"),
                   lambda p: p["base"]["repo"].update(full_name="foreign/mod"),
                   lambda p: p["base"].update(ref="other"), lambda p: p["base"].update(sha="f" * 40),
                   lambda p: p["head"].update(sha=True), lambda p: p["head"].update(ref="../unsafe")]
        for index, change in enumerate(changes):
            api, pr, path, controller = self.seed(draft=True, merge=False)
            change(pr)
            api.add_response(path, pr)
            with self.subTest(index=index), self.assertRaises(MbError):
                self.read(api, controller)


if __name__ == "__main__":
    unittest.main()
