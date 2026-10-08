"""Settlement of a merged batch: real Git, the real merged-gate readers, the fake API.

``World.merge`` merges the batch pull request as GitHub would and seeds both tested records; see
``tests/fixtures/ci_batch/support.py``.
"""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci import batch, transport
from mod_base.errors import MbError
from tests.fixtures.ci_batch.support import BASE_FILES, POSIX_ONLY, REPOSITORY, World, batch_plan
from tests.helpers import ci_api_artifact, ci_api_run
from tests.test_ci_batch import squash_by_hand

PULLS = f"/repos/{REPOSITORY}/pulls"
ISSUES = f"/repos/{REPOSITORY}/issues"
#: What ``transport.download_merged_gate_pair`` spends on the two gates of one pull request
#: (``tests/test_ci_merged_gate_pair.py`` says on what). The budget of ``ci batch-settle`` counts it.
GATE_PAIR_REQUESTS = 28


def gate_cost(world: World, arguments: dict) -> int:
    """Requests the merged-gate pair reader alone needs for this batch (another unit's code)."""

    start = world.hub.request_count
    master = world.remote.rev("refs/heads/master")
    transport.download_merged_gate_pair(
        world.hub, build_descriptor=arguments["build_seal"], packaged_descriptor=arguments["packaged_seal"],
        plan=arguments["plan"], controller_sha=master, merged_sha=master, temporary_root=world.state)
    return world.hub.request_count - start


@POSIX_ONLY
class SettleTests(unittest.TestCase):
    def merged(self, numbers=(1, 2), **options) -> tuple[World, dict, dict]:
        world = World(self)
        world.standard()
        result = world.prepare(numbers)
        arguments = world.merge(result, **options)
        world.hub.calls.clear()
        return world, arguments, result

    def refused(self, world: World, arguments: dict, message: str, **options) -> None:
        with self.assertRaisesRegex(MbError, message):
            world.settle(arguments, **options)
        self.assertEqual(world.hub.writes(), [])

    def test_members_still_at_their_batched_head_are_commented_and_closed(self):
        for parents in ("squash", "merge"):
            world, arguments, result = self.merged(parents=parents)
            self.assertEqual(gate_cost(world, arguments), GATE_PAIR_REQUESTS)
            world.hub.calls.clear()
            start = world.hub.request_count
            report = world.settle(arguments)
            self.assertEqual(report, {"closed": [1, 2], "changed": [], "already_closed": [], "deleted": []})
            self.assertEqual(world.hub.request_count - start, 3 + GATE_PAIR_REQUESTS + 3 * 2)  # 37
            reads = [path for method, path in world.hub.calls if method == "GET"]
            for immutable in ("/git/commits/", "/compare/", "/attempts/2/jobs"):
                self.assertEqual(sum(immutable in path for path in reads), 2, immutable)  # two objects, once each
            self.assertEqual(world.hub.writes(), [("POST", f"{ISSUES}/1/comments"), ("PATCH", f"{PULLS}/1"),
                                                  ("POST", f"{ISSUES}/2/comments"), ("PATCH", f"{PULLS}/2")])
            squash = result["manifest"]["members"][0]["squash_sha"]
            self.assertIn(("POST", f"{ISSUES}/1/comments",
                           {"body": f"Merged through batch #{result['pr_number']} (batch commit `{squash[:12]}`)."}),
                          world.hub.mutations)
            self.assertEqual([world.hub.get_json(f"{PULLS}/{number}")["state"] for number in (1, 2, 3)],
                             ["closed", "closed", "open"])
            self.assertIsNotNone(world.remote.rev("refs/heads/fix/alpha"))
            world.hub.calls.clear()
            self.assertEqual(world.settle(arguments, delete_branches=True),
                             {"closed": [], "changed": [], "already_closed": [1, 2], "deleted": []})
            self.assertEqual(world.hub.writes(), [])
            self.assertEqual(list(world.state.iterdir()), [])

    def test_settlement_holds_after_the_default_branch_moved_on(self):
        world, arguments, _ = self.merged()
        merged = world.remote.rev("refs/heads/master")
        later = world.remote.commit({**world.remote.files(merged), "later.txt": b"later\n"}, merged,
                                    message="chore: later")
        world.remote.set("refs/heads/master", later)
        # Without the ancestry of the merge in the new head, the merged-gate readers refuse.
        self.refused(world, arguments, "compare|Not Found")
        world.hub.add_compare(merged, later, {"status": "ahead", "ahead_by": 1, "behind_by": 0})
        world.hub.calls.clear()
        self.assertEqual(world.settle(arguments),
                         {"closed": [1, 2], "changed": [], "already_closed": [], "deleted": []})

    def test_a_member_with_later_commits_or_another_target_stays_open(self):
        world, arguments, _ = self.merged((1, 2, 3)[:2])
        world.move(2, "feat/beta", world.remote.commit({**BASE_FILES, "beta.txt": b"later\n"}, world.heads[2]))
        world.hub.pull(2, "feat/beta")
        self.assertEqual(world.settle(arguments), {"closed": [1], "changed": [2], "already_closed": [], "deleted": []})
        self.assertEqual(world.hub.get_json(f"{PULLS}/2")["state"], "open")
        for change in (lambda pull: pull["base"].update(ref="release/1.x"),
                       lambda pull: pull["head"].update(repo=None),
                       lambda pull: pull["head"]["repo"].update(full_name="someone/fork")):
            world, arguments, _ = self.merged()
            record = world.hub.pull(1, "fix/alpha")
            change(record)
            world.hub.add_pull(record)
            self.assertEqual(world.settle(arguments, delete_branches=True),
                             {"closed": [2], "changed": [1], "already_closed": [], "deleted": [2]})

    def test_a_branch_is_deleted_only_on_request_and_only_at_the_batched_head(self):
        world, arguments, _ = self.merged()
        moved = world.remote.commit({**BASE_FILES, "alpha.txt": b"later\n"}, world.heads[1])
        # The branch moves right after its pull request closed.
        world.hub.after("PATCH", f"{PULLS}/1", lambda: world.remote.set("refs/heads/fix/alpha", moved))
        batch_head = world.remote.rev("refs/heads/batch/tested")
        self.assertEqual(world.settle(arguments, delete_branches=True),
                         {"closed": [1, 2], "changed": [], "already_closed": [], "deleted": [2]})
        self.assertEqual(world.remote.rev("refs/heads/fix/alpha"), moved)
        self.assertIsNone(world.remote.rev("refs/heads/feat/beta"))
        self.assertEqual([call for call in world.hub.writes() if call[0] == "DELETE"],
                         [("DELETE", f"/repos/{REPOSITORY}/git/refs/heads/feat/beta")])
        # Neither the batch branch nor the default branch is ever a candidate.
        self.assertEqual(world.remote.rev("refs/heads/batch/tested"), batch_head)
        self.assertIsNotNone(world.remote.rev("refs/heads/master"))
        # A branch that is already gone is not an error.
        world, arguments, _ = self.merged()
        world.hub.after("PATCH", f"{PULLS}/1", lambda: world.hub.delete(f"/repos/{REPOSITORY}/git/refs/heads/fix/alpha"))
        self.assertEqual(world.settle(arguments, delete_branches=True),
                         {"closed": [1, 2], "changed": [], "already_closed": [], "deleted": [2]})
        # Without the flag no ref is read or deleted.
        world, arguments, _ = self.merged()
        self.assertEqual(world.settle(arguments)["deleted"], [])
        self.assertEqual([call for call in world.hub.calls if "/git/ref" in call[1]], [])
        self.assertIsNotNone(world.remote.rev("refs/heads/fix/alpha"))

    def test_a_push_that_races_the_close_reopens_the_pull_request(self):
        world, arguments, _ = self.merged()

        def push() -> None:
            world.move(1, "fix/alpha", world.remote.commit({**BASE_FILES, "alpha.txt": b"later\n"}, world.heads[1]))
            world.hub.pull(1, "fix/alpha")

        world.hub.after("POST", f"{ISSUES}/1/comments", push)
        self.assertEqual(world.settle(arguments, delete_branches=True),
                         {"closed": [2], "changed": [1], "already_closed": [], "deleted": [2]})
        self.assertEqual(world.hub.get_json(f"{PULLS}/1")["state"], "open")
        self.assertEqual(world.hub.writes()[:3], [("POST", f"{ISSUES}/1/comments"), ("PATCH", f"{PULLS}/1"),
                                                  ("PATCH", f"{PULLS}/1")])
        self.assertIsNotNone(world.remote.rev("refs/heads/fix/alpha"))

    def test_only_a_merged_batch_pull_request_with_its_own_marker_is_settled(self):
        def body(pull, change):
            pull["body"] = change(pull["body"])

        cases = {
            "open": (lambda pull: pull.update(state="open", merged=False), "has not merged"),
            "closed unmerged": (lambda pull: pull.update(merged=False), "has not merged"),
            "not a batch branch": (lambda pull: pull["head"].update(ref="fix/alpha"), "is not a batch pull request"),
            "foreign head": (lambda pull: pull["head"]["repo"].update(full_name="someone/fork"),
                             "is not a batch pull request"),
            "no merge commit": (lambda pull: pull.update(merge_commit_sha=None), "merged batch commit"),
            "no marker": (lambda pull: pull.update(body="## Summary\n"), "no single batch marker"),
            "two markers": (lambda pull: body(pull, lambda text: text + text), "no single batch marker"),
            "other branch": (lambda pull: pull["head"].update(ref="batch/other"), "describes another batch"),
            "other base": (lambda pull: pull["base"].update(ref="release/1.x"), "describes another batch"),
            "edited title": (lambda pull: body(pull, lambda text: text.replace(
                '"title":"fix: change fix/alpha"', '"title":"fix: change fix/other"')), "differs from the stack rebuilt"),
            "edited head": (lambda pull: body(pull, lambda text: text.replace('"pr_number":1', '"pr_number":3')),
                            "differs from the stack rebuilt"),
            "moved head": (lambda pull: pull["head"].update(sha="f" * 40), "did not test the stack"),
        }
        for label, (change, message) in cases.items():
            world, arguments, result = self.merged()
            pull = world.hub.get_json(f"{PULLS}/{result['pr_number']}")
            change(pull)
            world.hub.add_pull(pull)
            world.hub.calls.clear()
            with self.subTest(label=label):
                self.refused(world, arguments, message)

    def test_the_plan_must_be_the_one_whose_gates_tested_this_stack(self):
        world, arguments, result = self.merged()
        identity = arguments["plan"]["identity"]
        for label, change in (("number", {"pr_number": 3}), ("head", {"head_sha": world.heads[1],
                              "tested_parents": [identity["base_sha"], world.heads[1]]}),
                              ("tree", {"tested_tree": "9" * 40}), ("branch", {"head_branch": "batch/other"}),
                              ("base", {"base_sha": world.heads[3], "controller_sha": world.heads[3],
                                        "tested_parents": [world.heads[3], identity["head_sha"]]})):
            changed = {**arguments, "plan": batch_plan(**{**identity, **change})}
            with self.subTest(label=label):
                self.refused(world, changed, "plan")
        for label, changed in (("no plan", {**arguments, "plan": {}}),
                               ("own number", {**arguments, "pr_number": 1}),
                               ("bool", {**arguments, "pr_number": True})):
            with self.subTest(label=label), self.assertRaises(MbError):
                world.settle(changed)
        self.assertEqual(world.hub.writes(), [])
        with self.assertRaises(MbError):
            world.settle(arguments, delete_branches="yes")
        self.assertEqual(result["pr_number"], identity["pr_number"])

    def test_a_shorter_marker_than_the_merged_stack_is_refused(self):
        world, arguments, result = self.merged()
        manifest = copy.deepcopy(result["manifest"])
        manifest["members"].pop()
        manifest["result_tree"] = manifest["members"][-1]["result_tree"]
        pull = world.hub.get_json(f"{PULLS}/{result['pr_number']}")
        pull["body"] = batch.batch_marker(manifest) + "\n"
        world.hub.add_pull(pull)
        world.hub.calls.clear()
        self.refused(world, arguments, "did not test the stack of the batch marker")

    def test_a_merged_stack_with_a_smuggled_file_closes_nobody(self):
        world = World(self)
        world.standard()
        tree = world.remote.tree({**BASE_FILES, "alpha.txt": b"alpha\n", "src/evil.txt": b"evil\n"})
        forged = squash_by_hand(world, 1, "fix: change fix/alpha", tree, world.base)
        world.remote.set("refs/heads/batch/forged", forged)
        honest = World(self)
        honest.standard()
        manifest = honest.prepare([1], name="forged", dry_run=True)["manifest"]
        self.assertEqual(manifest["members"][0]["head_sha"], world.heads[1])
        manifest["members"][0].update(squash_sha=forged, result_tree=tree)
        manifest["result_tree"] = tree
        created = world.hub.post_json(PULLS, {"title": "chore: batch #1", "head": "batch/forged", "base": "master",
                                              "body": batch.batch_marker(manifest) + "\n", "draft": False})
        arguments = world.merge({"manifest": manifest, "pr_number": created["number"]})
        self.assertGreater(gate_cost(world, arguments), 0)  # both gates are authentic for that merge
        world.hub.calls.clear()
        self.refused(world, arguments, "differs from the stack rebuilt from its base and member heads")
        self.assertEqual(world.hub.get_json(f"{PULLS}/1")["state"], "open")

    def test_both_original_gates_must_authenticate_before_anyone_is_closed(self):
        world, arguments, _ = self.merged()
        plan, packaged_seal = arguments["plan"], arguments["packaged_seal"]
        # The packaged record is not the Build record, and only a managed caller's run seals one.
        self.refused(world, {**arguments, "build_seal": packaged_seal}, "exact required export or record")
        unmanaged = copy.deepcopy(packaged_seal)
        unmanaged["producer"]["workflow_path"] = ".github/workflows/on-demand-e2e.yml"
        self.refused(world, {**arguments, "packaged_seal": unmanaged}, "managed Build/E2E producer caller")
        # A run GitHub recorded at the controller on the base branch is not a gate of this batch:
        # the gates of a pull request are recorded under its head.
        world.hub.add_run(ci_api_run(plan, "packaged", head_sha=plan["identity"]["controller_sha"],
                                     head_branch="master"))
        self.refused(world, arguments, "not recorded under this subject's head")
        world.hub.add_run(ci_api_run(plan, "packaged"))
        # A tested record that expired can no longer be read.
        world.hub.add_artifact(ci_api_artifact(packaged_seal, expired=True), b"x")
        self.refused(world, arguments, "expired artifact")
        self.assertEqual(world.hub.get_json(f"{PULLS}/1")["state"], "open")

    def test_settlement_needs_a_writable_client_before_it_reads_anything(self):
        world, arguments, _ = self.merged()
        reader = World(self, writable=False)
        with reader.store() as store, self.assertRaisesRegex(MbError, "needs a writable GitHub client"):
            batch.settle_batch(reader.hub, store, **arguments)
        self.assertEqual(reader.hub.request_count, 0)


if __name__ == "__main__":
    unittest.main()
