"""The subject of a job that holds the candidate checkout: one request, proven equal, never fooled.

``identity.derive_subject`` runs on real Git checkouts, made the way ``actions/checkout`` makes them
(``tests/ci_checkout_fixture.py``), against a fake GitHub seeded with the facts of the repository
they were fetched from. Its record is compared byte for byte with the record
``identity.authenticate_subject`` returns from that same API, and every disagreement between the
run, the two checkouts and the live pull request must be a rejection.
"""

from __future__ import annotations

import copy
import json
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci import identity, planning
from mod_base.build_ci.checkout import CheckoutError, Commit
from mod_base.build_ci.protocol import plan_sha256, subject_of
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests import ci_checkout_fixture as fixture
from tests import ci_mod_harness as h
from tests.ci_lifecycle_fixture import git

PULL = fixture.PULL_REQUEST
REPOSITORY_PATH = f"/repos/{h.REPOSITORY}"
PULL_PATH = f"{REPOSITORY_PATH}/pulls/{PULL}"
BRANCH_PATH = f"{REPOSITORY_PATH}/branches/{h.BRANCH}"


class DerivationCase(fixture.GenerationCase):
    def environment(self, **options: str) -> dict[str, str]:
        return self.generation.environment(**options)

    def authenticate(self, api: FakeGitHub, *, producer: str = "build", pr_number: int | None = PULL,
                     environment: dict[str, str] | None = None) -> dict[str, Any]:
        """The record of the full authentication through the API, from the job's mod checkout."""

        environment = self.environment() if environment is None else environment
        return identity.authenticate_subject(fixture.invocation(self.mod, environment), api, producer=producer,
                                             pr_number=pr_number)

    def derive(self, api: FakeGitHub, *, producer: str = "build", pr_number: int | None = PULL,
               environment: dict[str, str] | None = None, mod: Path | None = None,
               candidate: Path | None = None) -> dict[str, Any]:
        """The record derived in a job whose checkouts are ``mod`` and ``candidate``."""

        environment = self.environment() if environment is None else environment
        return identity.derive_subject(fixture.invocation(self.mod if mod is None else mod, environment), api,
                                       producer=producer, pr_number=pr_number,
                                       candidate=self.candidate if candidate is None else candidate)

    def complete(self, commit: Commit, name: str = "complete") -> Path:
        """A candidate checkout with the whole history, at ``commit``: where a graft can be planted."""

        checkout = fixture.copy(self.generation.upstream, self.temporary / name / "candidate")
        git(checkout, "checkout", "-q", "--detach", commit.sha)
        return checkout


class PullRequestTests(DerivationCase):
    def test_the_derived_record_is_the_authenticated_one_byte_for_byte_in_one_request(self) -> None:
        tested, controller, head = self.generation.tested, self.generation.controller, self.generation.head
        for producer in ("build", "packaged"):
            environment = self.environment(caller=producer)
            full, _ = self.generation.github()
            authenticated = self.authenticate(full, producer=producer, environment=environment)
            api, _ = self.generation.github()
            derived = self.derive(api, producer=producer, environment=environment)
            with self.subTest(producer=producer):
                self.assertEqual(canonical_json(derived), canonical_json(authenticated))
                self.assertEqual(derived, authenticated)
                self.assertEqual(full.paths, [REPOSITORY_PATH, BRANCH_PATH, PULL_PATH,
                                              f"{REPOSITORY_PATH}/git/commits/{tested.sha}"])
                self.assertEqual(api.paths, [PULL_PATH])
                self.assertEqual((full.request_count, api.request_count, api.mutations), (4, 1, []))
                # Both say what the repository itself says of this generation.
                subject = derived["subject"]
                self.assertEqual((derived["producer"], derived["event"], derived["workflow_path"]),
                                 (producer, "pull_request_target", CI_CALLER_WORKFLOWS[producer]))
                self.assertEqual((derived["controller_tree"], subject["controller_sha"], subject["base_sha"]),
                                 (controller.tree, controller.sha, controller.sha))
                self.assertEqual((subject["tested_sha"], subject["tested_tree"], subject["tested_parents"]),
                                 (tested.sha, tested.tree, [controller.sha, head.sha]))
                self.assertEqual((subject["pr_number"], subject["head_sha"], subject["head_branch"],
                                  subject["base_branch"]), (PULL, head.sha, fixture.HEAD_BRANCH, h.BRANCH))

    def test_what_the_run_and_the_checkouts_claim_is_settled_before_the_one_request(self) -> None:
        def without(name: str) -> dict[str, str]:
            return {key: value for key, value in self.environment().items() if key != name}

        workflow = "GITHUB_WORKFLOW_REF"
        elsewhere = self.generation.checkout(self.temporary / "elsewhere" / "mod", self.generation.head.sha)
        renamed = fixture.copy(self.mod, self.temporary / "renamed" / "mod")
        config = renamed / "site" / "mod-base.json"
        config.write_bytes(h.pretty({**json.loads(config.read_bytes()), "canonical_branch": "release"}))
        cases: dict[str, dict[str, Any]] = {
            "a push event": {"environment": self.environment(event="push")},
            "an unknown event": {"environment": self.environment(event="pull_request")},
            "no event": {"environment": without("GITHUB_EVENT_NAME")},
            "the packaged caller": {"environment": self.environment(caller="packaged")},
            "an unmanaged caller": {"environment": {
                **self.environment(), workflow: f"{h.REPOSITORY}/.github/workflows/ci.yml@refs/heads/main"}},
            "a caller of another repository": {"environment": {
                **self.environment(), workflow: f"other/mod/{CI_CALLER_WORKFLOWS['build']}@refs/heads/main"}},
            "a caller of another branch": {"environment": {
                **self.environment(), workflow: f"{h.REPOSITORY}/{CI_CALLER_WORKFLOWS['build']}@refs/heads/release"}},
            "a caller at a tag": {"environment": {
                **self.environment(), workflow: f"{h.REPOSITORY}/{CI_CALLER_WORKFLOWS['build']}@refs/tags/v1"}},
            "no caller": {"environment": without(workflow)},
            "a run of another branch": {"environment": {**self.environment(), "GITHUB_REF": "refs/heads/release"}},
            "a run of the pull request ref": {"environment": {
                **self.environment(), "GITHUB_REF": f"refs/pull/{PULL}/merge"}},
            "no ref": {"environment": without("GITHUB_REF")},
            "no kit": {"environment": without("MOD_BASE_KIT_SHA")},
            "no controller": {"environment": without("GITHUB_SHA")},
            "another repository": {"environment": {**self.environment(), "GITHUB_REPOSITORY": "example/other"}},
            "a mod with another canonical branch": {"mod": renamed},
            "a run that executes another commit": {"environment": {
                **self.environment(), "GITHUB_SHA": self.generation.head.sha}},
            "a mod checkout at another commit": {"mod": elsewhere},
            "a mod that is no checkout": {"mod": h.materialize(self.temporary / "plain" / "mod")},
            "a candidate that is no checkout": {"candidate": h.materialize(self.temporary / "plain" / "candidate")},
            "no candidate": {"candidate": self.temporary / "absent"},
            "a status job": {"producer": "status", "environment": self.environment(caller="status")},
            "an unknown producer": {"producer": "release"},
            "no producer": {"producer": None},
        }
        for label, options in cases.items():
            api, _ = self.generation.github()
            with self.subTest(case=label), self.assertRaises(MbError):
                self.derive(api, **options)
            self.assertEqual(api.request_count, 0, label)
        api, _ = self.generation.github()
        with self.assertRaisesRegex(MbError, "the mod checkout is not at the executing controller commit"):
            self.derive(api, mod=elsewhere)
        with self.assertRaisesRegex(CheckoutError, "the mod checkout has no Git directory of its own"):
            self.derive(api, mod=self.temporary / "plain" / "mod")
        with self.assertRaisesRegex(MbError, "only a Build or packaged job holds a candidate checkout"):
            self.derive(api, producer="status", environment=self.environment(caller="status"))
        self.assertEqual(api.request_count, 0)
        # The status job authenticates this very pull request in full, as before.
        self.assertEqual(self.authenticate(api, producer="status", environment=self.environment(caller="status"))
                         ["subject"], self.derive(self.generation.github()[0])["subject"])

    def test_a_candidate_checkout_at_another_commit_is_a_rejection(self) -> None:
        tested = self.generation.tested
        again = self.generation.commit_tree(tested.tree, *tested.parents, message="the same merge, made again")
        self.assertEqual((again.tree, again.parents), (tested.tree, tested.parents))
        for label, commit in (("the head", self.generation.head), ("the controller", self.generation.controller),
                              ("an equal merge that is another commit", again)):
            candidate = self.generation.checkout(self.temporary / label / "candidate", commit.sha)
            api, _ = self.generation.github(again)
            with self.subTest(candidate=label), self.assertRaisesRegex(
                    MbError, "the candidate checkout is not at the current test merge of the pull request"):
                self.derive(api, candidate=candidate)
            self.assertEqual(api.paths, [PULL_PATH], label)

    def test_the_test_merge_must_have_exactly_the_controller_then_the_head_as_parents(self) -> None:
        tested, controller, head, base = (self.generation.tested, self.generation.controller, self.generation.head,
                                          self.generation.base)
        orders = {"reversed": (head, controller), "the controller alone": (controller,),
                  "another second parent": (controller, base), "another first parent": (base, head),
                  "a root commit": (), "three parents": (controller, head, base)}
        for label, parents in orders.items():
            merge = self.generation.commit_tree(tested.tree, *(parent.sha for parent in parents), message=label)
            self.assertEqual(merge.parents, tuple(parent.sha for parent in parents))
            candidate = self.generation.checkout(self.temporary / label / "candidate", merge.sha)
            for function, requests in ((self.authenticate, 4), (lambda api: self.derive(api, candidate=candidate), 1)):
                # The pull request names this commit as its test merge: only its parents are wrong.
                api, pull = self.generation.github(merge)
                h.seed_pull_request(api, {**pull, "merge_commit_sha": merge.sha})
                with self.subTest(parents=label, requests=requests), self.assertRaises(MbError) as caught:
                    function(api)
                self.assertIn("parents", str(caught.exception))
                self.assertEqual(api.request_count, requests)

    def test_a_pull_request_that_is_no_longer_what_the_run_tests_is_a_rejection(self) -> None:
        tested, controller, base = self.generation.tested, self.generation.controller, self.generation.base
        moved = self.generation.commit_tree(self.generation.head.tree, base.sha, message="the head, pushed again")
        merged = self.generation.commit_tree(tested.tree, controller.sha, moved.sha, message="its test merge")
        again = self.generation.commit_tree(tested.tree, *tested.parents, message="the same merge, made again")

        def change(*path: str, value: Any) -> Callable[[dict[str, Any]], None]:
            def apply(pull: dict[str, Any]) -> None:
                target = pull
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

            return apply

        def both(first: Callable[[dict[str, Any]], None],
                 second: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
            def apply(pull: dict[str, Any]) -> None:
                first(pull)
                second(pull)

            return apply

        cases: dict[str, tuple[Callable[[dict[str, Any]], None], str | None, str]] = {
            "closed": (change("state", value="closed"), None, "open PR"),
            "merged": (both(change("state", value="closed"), change("merged", value=True)), None, "open PR"),
            "a draft": (change("draft", value=True), "draft", "is a draft"),
            "no readiness state": (change("draft", value=None), None, "exact readiness state"),
            "no test merge": (change("merge_commit_sha", value=None), "no-test-merge", "has no test merge"),
            "a moved head": (change("head", "sha", value=moved.sha), None, "parents"),
            "a moved head with its new test merge": (both(change("head", "sha", value=moved.sha),
                                                          change("merge_commit_sha", value=merged.sha)),
                                                     None, "not at the current test merge"),
            "a regenerated test merge": (change("merge_commit_sha", value=again.sha), None,
                                         "not at the current test merge"),
            "a head in a fork": (change("head", "repo", "full_name", value="fork/synthetic-mod"), None, "fork"),
            "another base branch": (change("base", "ref", value="release"), None, "PR base differs"),
            "another base commit": (change("base", "sha", value=base.sha), None, "PR base differs"),
            "a base in another repository": (change("base", "repo", "full_name", value="example/other"), None,
                                             "fork"),
            "another pull request": (change("number", value=PULL + 1), None, "wrong or malformed PR"),
            "a malformed head": (change("head", "sha", value="HEAD"), None, "PR head SHA"),
            "a malformed test merge": (change("merge_commit_sha", value="merge"), None, "PR merge SHA"),
        }
        for label, (apply, reason, message) in cases.items():
            api, pull = self.generation.github(merged, again)
            changed = copy.deepcopy(pull)
            apply(changed)
            api.add_response(PULL_PATH, changed)
            with self.subTest(case=label), self.assertRaises(MbError) as caught:
                self.derive(api)
            self.assertIn(message, str(caught.exception), label)
            self.assertEqual(api.paths, [PULL_PATH], label)
            if reason is not None:
                self.assertEqual((type(caught.exception), caught.exception.reason), (identity.SubjectError, reason))
        api, _ = self.generation.github()
        with self.assertRaises(MbError):
            self.derive(api, pr_number=PULL + 1)
        self.assertEqual(api.paths, [f"{REPOSITORY_PATH}/pulls/{PULL + 1}"], "a pull request the repository lacks")

    def test_a_pull_request_behind_its_base_needs_a_new_push_in_both_routes(self) -> None:
        # GitHub retains pulls/{n}.base.sha from the last head push. A recomputed test merge
        # alone does not make that older generation eligible under the protected controller.
        for function, requests in ((self.authenticate, 3), (self.derive, 1)):
            api, pull = self.generation.github()
            h.seed_pull_request(api, {**pull, "base": {**pull["base"], "sha": self.generation.base.sha}})
            with self.subTest(requests=requests), self.assertRaises(MbError) as caught:
                function(api)
            self.assertIn("update the pull request branch and push it again", str(caught.exception))
            self.assertEqual(api.request_count, requests)

    def test_the_head_branch_is_the_live_one(self) -> None:
        records = []
        for function in (self.authenticate, self.derive):
            api, pull = self.generation.github()
            h.seed_pull_request(api, {**pull, "head": {**pull["head"], "ref": "feature/renamed"}})
            records.append(function(api))
        self.assertEqual(canonical_json(records[1]), canonical_json(records[0]))
        self.assertEqual(records[1]["subject"]["head_branch"], "feature/renamed")


class ProtectedSubjectTests(DerivationCase):
    def setUp(self) -> None:
        super().setUp()
        # A protected subject is its own candidate: both checkouts are at the executing commit.
        self.candidate = fixture.copy(self.mod, self.temporary / "protected" / "candidate")

    def test_the_derived_record_is_the_authenticated_one_byte_for_byte_in_one_request(self) -> None:
        controller = self.generation.controller
        cases = [(event, "build", "build") for event in identity.PROTECTED_EVENTS]
        # The rebuild job of the packaged caller runs Build jobs, and the packaged run has lanes.
        cases += [("workflow_dispatch", "build", "packaged"), ("push", "packaged", "packaged")]
        for event, producer, caller in cases:
            environment = self.environment(event=event, caller=caller)
            full, _ = self.generation.github()
            authenticated = self.authenticate(full, producer=producer, pr_number=None, environment=environment)
            api, _ = self.generation.github()
            derived = self.derive(api, producer=producer, pr_number=None, environment=environment)
            with self.subTest(event=event, producer=producer, caller=caller):
                self.assertEqual(canonical_json(derived), canonical_json(authenticated))
                self.assertEqual(full.paths, [REPOSITORY_PATH, BRANCH_PATH,
                                              f"{REPOSITORY_PATH}/git/commits/{controller.sha}", REPOSITORY_PATH,
                                              BRANCH_PATH])
                self.assertEqual(api.paths, [BRANCH_PATH])
                self.assertEqual((full.request_count, api.request_count, api.mutations), (5, 1, []))
                subject = derived["subject"]
                self.assertEqual((derived["producer"], derived["event"], derived["workflow_path"]),
                                 (producer, event, CI_CALLER_WORKFLOWS[caller]))
                self.assertEqual({subject["head_sha"], subject["tested_sha"], subject["controller_sha"],
                                  subject["base_sha"]}, {controller.sha})
                self.assertEqual((subject["pr_number"], subject["tested_tree"], subject["tested_parents"],
                                  derived["controller_tree"], subject["head_branch"]),
                                 (0, controller.tree, [self.generation.base.sha], controller.tree, h.BRANCH))

    def test_a_protected_subject_is_the_commit_both_checkouts_are_at(self) -> None:
        push = self.environment(event="push")
        merge = fixture.copy(self._candidate, self.temporary / "merge" / "candidate")
        cases: dict[str, dict[str, Any]] = {
            "a candidate at the test merge of a pull request": {"candidate": merge},
            "a candidate at the head of a pull request": {"candidate": self.generation.checkout(
                self.temporary / "head" / "candidate", self.generation.head.sha)},
            "a pull request event": {"environment": self.environment()},
            "a packaged job in the Build caller": {"producer": "packaged"},
            "a status job": {"producer": "status", "environment": self.environment(event="schedule", caller="status")},
        }
        for label, options in cases.items():
            api, _ = self.generation.github()
            with self.subTest(case=label), self.assertRaises(MbError):
                self.derive(api, **{"pr_number": None, "environment": push, **options})
            self.assertEqual(api.request_count, 0, label)
        api, _ = self.generation.github()
        with self.assertRaisesRegex(MbError, "candidate checkout of a protected subject is not at the executing"):
            self.derive(api, pr_number=None, environment=push, candidate=merge)
        with self.assertRaises(MbError):
            self.derive(api, pr_number=PULL, environment=push)
        self.assertEqual(api.request_count, 0)

    def test_a_default_branch_that_moved_or_disagrees_is_a_rejection(self) -> None:
        controller, head = self.generation.controller, self.generation.head
        push = self.environment(event="push")

        def response(**changes: Any) -> dict[str, Any]:
            return {"name": h.BRANCH, "commit": {"sha": controller.sha, "commit": {"tree": {"sha": controller.tree}}},
                    **changes}

        api, _ = self.generation.github()
        api.set_branch(h.BRANCH, head.sha, head.tree)
        with self.assertRaises(identity.SubjectError) as caught:
            self.derive(api, pr_number=None, environment=push)
        self.assertEqual((caught.exception.reason, api.paths), ("controller-moved", [BRANCH_PATH]))
        responses = {
            "another tree for the same commit": response(commit={"sha": controller.sha,
                                                                 "commit": {"tree": {"sha": head.tree}}}),
            "another branch": response(name="release"),
            "no tree": response(commit={"sha": controller.sha}),
            "not a branch": [h.BRANCH],
        }
        for label, payload in responses.items():
            api, _ = self.generation.github()
            api.add_response(BRANCH_PATH, payload)
            with self.subTest(response=label), self.assertRaises(MbError):
                self.derive(api, pr_number=None, environment=push)
            self.assertEqual(api.paths, [BRANCH_PATH], label)
        api.add_response(BRANCH_PATH, response())
        self.assertEqual(self.derive(api, pr_number=None, environment=push)["subject"]["tested_sha"], controller.sha)
        absent = fixture.RecordingGitHub(repository=h.REPOSITORY, default_branch=h.BRANCH)
        with self.assertRaises(MbError):
            self.derive(absent, pr_number=None, environment=push)
        self.assertEqual(absent.paths, [BRANCH_PATH], "a branch the repository lacks")


class HostileCheckoutTests(DerivationCase):
    """What Git would read differently in the candidate checkout never reaches the record."""

    def wrong_merge(self) -> Commit:
        """A commit the API names as the test merge although its first parent is not the controller."""

        return self.generation.commit_tree(self.generation.tested.tree, self.generation.base.sha,
                                           self.generation.head.sha, message="merged into another base")

    def test_a_replacement_or_a_graft_that_makes_the_right_commit_read_wrongly_changes_nothing(self) -> None:
        tested, base, wrong = self.generation.tested, self.generation.base, self.wrong_merge()
        authenticated = canonical_json(self.authenticate(self.generation.github()[0]))
        true = f"{tested.sha} {tested.parents[0]} {tested.parents[1]}"

        replaced = self.complete(tested, "replaced")
        git(replaced, "replace", tested.sha, wrong.sha)
        self.assertEqual(git(replaced, "rev-list", "--parents", "--max-count=1", "HEAD"),
                         f"{tested.sha} {base.sha} {self.generation.head.sha}")

        grafted = self.complete(tested, "grafted")
        self.assertEqual(git(grafted, "rev-list", "--parents", "--max-count=1", "HEAD"), true)
        (grafted / ".git" / "info").mkdir(exist_ok=True)
        (grafted / ".git" / "info" / "grafts").write_bytes(f"{tested.sha} {base.sha}\n".encode("ascii"))
        self.assertEqual(git(grafted, "rev-list", "--parents", "--max-count=1", "HEAD"), f"{tested.sha} {base.sha}",
                         "this Git no longer honours a grafts file: the case proves nothing any more")

        # The checkout of a job is shallow: there Git reports no parent at all.
        self.assertEqual(git(self.candidate, "rev-list", "--parents", "--max-count=1", "HEAD"), tested.sha)
        packed = self.generation.checkout(self.temporary / "packed" / "candidate", tested.sha, packed=True)
        for label, candidate in (("a replacement ref", replaced), ("a grafts file", grafted),
                                 ("a shallow boundary", self.candidate), ("a pack", packed)):
            api, _ = self.generation.github()
            with self.subTest(candidate=label):
                self.assertEqual(canonical_json(self.derive(api, candidate=candidate)), authenticated)
                self.assertEqual(api.paths, [PULL_PATH])

    def test_a_replacement_or_a_graft_that_makes_a_wrong_commit_read_rightly_is_still_a_rejection(self) -> None:
        tested, wrong = self.generation.tested, self.wrong_merge()
        right = f"{wrong.sha} {tested.parents[0]} {tested.parents[1]}"

        replaced = self.complete(wrong, "replaced")
        git(replaced, "replace", wrong.sha, tested.sha)
        grafted = self.complete(wrong, "grafted")
        (grafted / ".git" / "info").mkdir(exist_ok=True)
        (grafted / ".git" / "info" / "grafts").write_bytes(f"{right}\n".encode("ascii"))
        for label, candidate in (("a replacement ref", replaced), ("a grafts file", grafted)):
            # To plain Git the commit now has the controller and the head as its parents.
            self.assertEqual(git(candidate, "rev-list", "--parents", "--max-count=1", "HEAD"), right, label)
            api, pull = self.generation.github(wrong)
            h.seed_pull_request(api, {**pull, "merge_commit_sha": wrong.sha})
            with self.subTest(candidate=label), self.assertRaises(MbError) as caught:
                self.derive(api, candidate=candidate)
            self.assertIn("must equal the ordered base/head parents", str(caught.exception))
            self.assertEqual(api.paths, [PULL_PATH])
        # The API says the same of this commit, so the full authentication refuses it as well.
        api, pull = self.generation.github(wrong)
        h.seed_pull_request(api, {**pull, "merge_commit_sha": wrong.sha})
        with self.assertRaisesRegex(MbError, "must equal the ordered base/head parents"):
            self.authenticate(api)

    def test_an_object_stored_under_the_name_of_the_test_merge_is_a_rejection(self) -> None:
        tested, controller, wrong = self.generation.tested, self.generation.controller, self.wrong_merge()
        # The right parents with another tree, under the name of the real test merge.
        fixture.write_object(self.candidate, "commit", fixture.commit_bytes(controller.tree, tested.parents),
                             name=tested.sha)
        api, _ = self.generation.github()
        with self.assertRaisesRegex(CheckoutError, "the commit read at HEAD of the candidate checkout does not hash"):
            self.derive(api)
        self.assertEqual(api.request_count, 0)
        # The right parents under the name of a commit that has other parents.
        candidate = self.generation.checkout(self.temporary / "wrong" / "candidate", wrong.sha)
        fixture.write_object(candidate, "commit", fixture.commit_bytes(tested.tree, tested.parents), name=wrong.sha)
        api, pull = self.generation.github(wrong)
        h.seed_pull_request(api, {**pull, "merge_commit_sha": wrong.sha})
        with self.assertRaisesRegex(CheckoutError, "does not hash to its name"):
            self.derive(api, candidate=candidate)
        self.assertEqual(api.request_count, 0)

    def test_an_object_stored_under_the_name_of_the_controller_is_a_rejection(self) -> None:
        controller = self.generation.controller
        fixture.write_object(self.mod, "commit", fixture.commit_bytes(self.generation.tested.tree, controller.parents),
                             name=controller.sha)
        for pr_number, environment in ((PULL, self.environment()), (None, self.environment(event="push"))):
            api, _ = self.generation.github()
            with self.subTest(pr_number=pr_number), self.assertRaisesRegex(
                    CheckoutError, "the commit read at HEAD of the mod checkout does not hash to its name"):
                self.derive(api, pr_number=pr_number, environment=environment)
            self.assertEqual(api.request_count, 0)


class PlanTests(DerivationCase):
    def plan(self, label: str, subject: dict[str, Any]) -> dict[str, Any]:
        """The plan the synthetic mod's own ``derive_plan`` hook yields for ``subject``."""

        sandbox = h.Sandbox(self.temporary / label, protected=self.mod, candidate=self.candidate)
        sandbox.subject = subject
        return sandbox.derive_plan()

    def test_a_plan_of_the_derived_subject_has_the_hash_of_the_plan_of_the_authenticated_one(self) -> None:
        authenticated = self.authenticate(self.generation.github()[0])["subject"]
        derived = self.derive(self.generation.github()[0])["subject"]
        first, second = self.plan("authenticated", authenticated), self.plan("derived", derived)
        self.assertEqual(second["plan_sha256"], first["plan_sha256"])
        self.assertEqual(canonical_json(second), canonical_json(first))
        self.assertEqual(subject_of(second["identity"]), derived)
        # What `ci plan --expect-sha256` requires of the job that derived its subject.
        self.assertIs(planning.require_plan(second, subject=derived, expected_sha256=first["plan_sha256"]), second)

    def test_the_plan_hash_covers_every_field_of_the_subject(self) -> None:
        # The derivation takes the base branch from the run and does not read the head of the
        # default branch again for a pull request. Neither can differ unnoticed from what the
        # first job authenticated: a subject that differs anywhere has another plan hash.
        subject = self.derive(self.generation.github()[0])["subject"]
        plan = self.plan("derived", subject)
        other = "9" * 40
        changes: dict[str, Any] = {
            "repository": "example/other", "source_repository": "example/other", "pr_number": PULL + 1,
            "head_sha": other, "head_branch": "feature/other", "base_sha": other, "base_branch": "release",
            "controller_sha": other, "controller_workflow": CI_CALLER_WORKFLOWS["packaged"],
            "controller_ref": grammar.workflow_ref(h.REPOSITORY, CI_CALLER_WORKFLOWS["build"], "release"),
            "kit": {**subject["kit"], "sha": other}, "tested_sha": other, "tested_tree": other,
            "tested_parents": list(reversed(subject["tested_parents"])), "graph_version": 2,
        }
        self.assertEqual(set(changes), set(subject))
        hashes = {plan["plan_sha256"]}
        for field, value in changes.items():
            changed = copy.deepcopy(plan)
            changed["identity"][field] = value
            hashes.add(plan_sha256(changed))
        self.assertEqual(len(hashes), 1 + len(subject))
        # A job that took another base branch from its run would be stopped by the plan it must reproduce.
        moved = copy.deepcopy(plan)
        moved["identity"].update(base_branch="release", controller_ref=changes["controller_ref"])
        moved["plan_sha256"] = plan_sha256(moved)
        with self.assertRaises(planning.PlanError) as caught:
            planning.require_plan(moved, subject=subject_of(moved["identity"]), expected_sha256=plan["plan_sha256"])
        self.assertEqual(caught.exception.reason, "plan-mismatch")


if __name__ == "__main__":
    unittest.main()
