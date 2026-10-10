"""Batch construction and verification: the fake API in front of a real repository.

``tests/fixtures/ci_batch/support.py`` explains the fixture. Only GitHub is replaced; the Git
writer, the manifest, the marker and every check run for real.
"""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci import batch
from mod_base.build_ci.batch_git import BOT_EMAIL, BOT_NAME
from mod_base.errors import MbError
from mod_base.github.api import ApiError
from mod_base.model import limits
from tests.fixtures.ci_batch.support import ALLOWED, BASE_FILES, POSIX_ONLY, REPOSITORY, World

PULLS = f"/repos/{REPOSITORY}/pulls"
REPO = f"/repos/{REPOSITORY}"
REF = f"/repos/{REPOSITORY}/git/ref/heads/batch/tested"
BOT = (BOT_NAME, BOT_EMAIL)


def squash_by_hand(world: World, number: int, title: str, tree: str, parent: str) -> str:
    """The squash commit of a member, written on the remote as anyone with push access could."""

    return world.remote.commit_tree(tree, parent, identity=BOT,
                                    message=f"{title} (#{number})\n\nBatch-Member: {number} {world.heads[number]}")


@POSIX_ONLY
class PrepareTests(unittest.TestCase):
    def test_prepare_squashes_in_order_pushes_a_new_branch_and_opens_one_ready_pull_request(self):
        world = World(self)
        world.standard()
        result = world.prepare([2, 1])
        manifest = result["manifest"]
        head = manifest["members"][-1]["squash_sha"]
        self.assertEqual(world.remote.rev("refs/heads/batch/tested"), head)
        self.assertEqual(world.remote.rev(head + "^"), manifest["members"][0]["squash_sha"])
        self.assertEqual(world.remote.rev(head + "~2"), world.base)
        self.assertEqual([(member["pr_number"], member["head_sha"], member["title"]) for member in manifest["members"]],
                         [(2, world.heads[2], "fix: change feat/beta"), (1, world.heads[1], "fix: change fix/alpha")])
        self.assertEqual((manifest["repository"], manifest["base_branch"], manifest["base_sha"], manifest["branch"]),
                         (REPOSITORY, "master", world.base, "batch/tested"))
        self.assertEqual(manifest["result_tree"], world.remote.rev(head + "^{tree}"))
        self.assertEqual(world.remote.files(head), {**BASE_FILES, "alpha.txt": b"alpha\n", "beta.txt": b"beta\n",
                                                    "shared.txt": b"one\ntwo\nthree\nfour\n"})
        self.assertEqual(world.hub.writes(), [("POST", PULLS)])
        (method, path, payload), = world.hub.mutations
        self.assertEqual({key: payload[key] for key in ("title", "head", "base", "draft")},
                         {"title": "chore: batch #2, #1", "head": "batch/tested", "base": "master", "draft": False})
        self.assertEqual(batch.read_batch_marker(payload["body"]), manifest)
        created = world.hub.get_json(f"{PULLS}/{result['pr_number']}")
        self.assertEqual((created["state"], created["draft"], created["head"]["sha"]), ("open", False, head))
        self.assertEqual((result["dry_run"], world.hub.request_count), (False, 3 * 2 + 10 + 1))
        self.assertEqual(list(world.state.iterdir()), [])

    def test_a_dry_run_builds_the_same_batch_with_a_read_only_client_and_writes_nothing(self):
        world = World(self, writable=False)
        world.standard()
        refs = world.remote.refs()
        dry = world.prepare([2, 1], dry_run=True)
        self.assertEqual((dry["dry_run"], dry["pr_number"], world.hub.request_count), (True, None, 2 + 3))
        self.assertEqual((world.remote.refs(), world.hub.mutations, world.hub.writes()), (refs, [], []))
        with self.assertRaisesRegex(MbError, "needs a writable GitHub client"):
            world.prepare([2, 1])
        self.assertEqual((world.remote.refs(), world.hub.request_count), (refs, 5))
        real = World(self)
        real.standard()
        self.assertEqual(real.prepare([2, 1])["manifest"], dry["manifest"])

    def test_only_open_same_repository_pull_requests_to_the_default_branch_are_batched(self):
        mutations = {
            "fork": lambda pull: pull["head"]["repo"].update(full_name="someone/fork"),
            "deleted fork": lambda pull: pull["head"].update(repo=None),
            "closed": lambda pull: pull.update(state="closed"),
            "merged": lambda pull: pull.update(merged_at="2026-10-08T10:00:00Z"),
            "other base": lambda pull: pull["base"].update(ref="release/1.x"),
            "foreign base": lambda pull: pull["base"]["repo"].update(full_name="someone/else"),
            "batch head": lambda pull: pull["head"].update(ref="batch/older"),
            "base head": lambda pull: pull["head"].update(ref="master"),
            "bad head": lambda pull: pull["head"].update(sha="f" * 39),
            "line break": lambda pull: pull.update(title="fix:\nnewline"),
            "empty title": lambda pull: pull.update(title=""),
            "long title": lambda pull: pull.update(title="t" * (limits.MAX_CI_BATCH_TITLE_CHARS + 1)),
            "no title": lambda pull: pull.pop("title"),
            "other number": lambda pull: pull.update(number=7),
            "not a pull": lambda pull: pull.clear() or pull.update(number=2, message="Not Found"),
        }
        for label, mutate in mutations.items():
            world = World(self)
            world.standard()
            record = world.hub.pull(2, "feat/beta")
            mutate(record)
            if label in ("other number", "not a pull"):
                world.hub.add_response(f"{PULLS}/2", record)
            else:
                world.hub.add_pull(record)
            refs = world.remote.refs()
            with self.subTest(label=label), self.assertRaises(MbError):
                world.prepare([1, 2])
            self.assertEqual((world.remote.refs(), world.hub.writes()), (refs, []))
        world = World(self)
        world.standard()
        with self.assertRaises(MbError):
            world.prepare([1, 9])  # no such pull request: the API answers 404

    def test_member_numbers_name_and_policy_are_checked_before_any_request(self):
        world = World(self)
        world.standard()
        for numbers in ((), (1, 1), (0,), (True,), ("1",), tuple(range(1, limits.MAX_CI_BATCH_MEMBERS + 2))):
            with self.subTest(numbers=numbers[:3]), self.assertRaises(MbError):
                world.prepare(numbers)
        with world.store() as store, self.assertRaises(MbError):
            batch.prepare_batch(world.hub, store, name="tested", pr_numbers=[1, 2], allowed_paths=ALLOWED)
        for name in ("../master", "UPPER", "", "a/b", "x.lock", "x.", "-x", "n" * 64, None):
            with self.subTest(name=name), self.assertRaises(MbError):
                world.prepare([1], name=name)
        for allowed in ((), ("src", "alpha.txt"), ["src"], ("../src",)):
            with self.subTest(allowed=allowed), self.assertRaises(MbError):
                world.prepare([1], allowed_paths=allowed)
        with world.store() as store, self.assertRaises(MbError):
            batch.prepare_batch(world.hub, store, name="tested", pr_numbers=(1,), allowed_paths=ALLOWED, dry_run=1)
        self.assertEqual((world.hub.request_count, world.hub.calls), (0, []))
        self.assertEqual(world.prepare(tuple([1]), name="2026-10-08.x_1")["manifest"]["branch"], "batch/2026-10-08.x_1")

    def test_conflicts_empty_members_and_forbidden_paths_publish_nothing(self):
        world = World(self)
        world.standard()
        world.member(4, "fix/workflow", {**BASE_FILES, ".github/workflows/ci.yml": b"on: push\n"})
        world.member(5, "fix/again", {**BASE_FILES, "alpha.txt": b"alpha\n"})
        for numbers, message in (((2, 3), r"#3 conflicts with master or an earlier batch entry: shared\.txt"),
                                 ((1, 5), "#5 adds nothing on top of master and earlier batch entries"),
                                 ((1, 4), r"#4 changes a path the protected policy does not allow in a batch: \.github/")):
            with self.subTest(numbers=numbers), self.assertRaisesRegex(MbError, message):
                world.prepare(numbers)
        self.assertEqual(world.hub.writes(), [])
        self.assertIsNone(world.remote.rev("refs/heads/batch/tested"))

    def test_a_head_or_base_that_moved_before_the_fetch_is_noticed_by_git(self):
        for label, path, message in (("head", f"{PULLS}/2", "#2 moved while the batch was being prepared"),
                                     ("base", f"{REPO}/branches/master", "master moved while the batch was being")):
            world = World(self)
            world.standard()

            def move(world=world, label=label):
                if label == "head":
                    world.remote.set("refs/pull/2/head", world.remote.commit({**BASE_FILES, "beta.txt": b"2\n"},
                                                                             world.heads[2]))
                else:
                    world.remote.set("refs/heads/master", world.remote.commit({**BASE_FILES, "keep.txt": b"k\n"},
                                                                              world.base))

            world.hub.after("GET", path, move)
            with self.subTest(label=label), self.assertRaisesRegex(MbError, message):
                world.prepare([1, 2])
            self.assertEqual(world.hub.writes(), [])
            self.assertIsNone(world.remote.rev("refs/heads/batch/tested"))

    def test_a_member_or_base_that_changes_before_the_push_stops_the_batch(self):
        changes = {
            "head": (lambda world: (world.move(2, "feat/beta", world.remote.commit(
                {**BASE_FILES, "beta.txt": b"2\n"}, world.heads[2])), world.hub.pull(2, "feat/beta")),
                "#2 changed while the batch was being prepared; run it again"),
            "title": (lambda world: world.hub.pull(1, "fix/alpha", title="fix: renamed"), "#1 changed while"),
            "closed": (lambda world: world.hub.pull(1, "fix/alpha", state="closed"), "#1 is not open"),
            "base": (lambda world: world.remote.set("refs/heads/master", world.remote.commit(
                {**BASE_FILES, "keep.txt": b"k\n"}, world.base)), "master moved while the batch was being prepared"),
            "branch": (lambda world: world.remote.set("refs/heads/batch/tested", world.base),
                       "batch/tested already exists"),
        }
        for label, (change, message) in changes.items():
            world = World(self)
            world.standard()
            # The second read of the repository opens the revalidation that precedes the push.
            world.hub.after("GET", REPO, lambda world=world, change=change: change(world), nth=2)
            with self.subTest(label=label), self.assertRaisesRegex(MbError, message):
                world.prepare([1, 2])
            self.assertEqual(world.hub.writes(), [])
            self.assertIn(world.remote.rev("refs/heads/batch/tested"), (None, world.base))

    def test_an_existing_branch_is_refused_before_anything_is_fetched(self):
        world = World(self)
        world.standard()
        world.remote.set("refs/heads/batch/tested", world.base)
        with self.assertRaisesRegex(MbError, "batch/tested already exists; a batch never reuses a branch"):
            world.prepare([1, 2])
        # The first look: the repository, its head, both members and the branch.
        self.assertEqual((world.hub.request_count, world.hub.writes()), (2 + 2 + 1, []))
        self.assertEqual(world.remote.rev("refs/heads/batch/tested"), world.base)

    def test_a_branch_that_is_not_the_pushed_commit_gets_no_pull_request(self):
        world = World(self)
        world.standard()
        # Someone moves the new branch between the push and the last look at it.
        world.hub.after("GET", f"{PULLS}/2", lambda: world.remote.set("refs/heads/batch/tested", world.heads[3]),
                        nth=3)
        with self.assertRaisesRegex(MbError, "batch/tested is not the pushed batch commit"):
            world.prepare([1, 2])
        self.assertEqual(world.hub.writes(), [])

    def test_a_branch_that_appears_after_the_last_read_is_never_replaced_or_adopted(self):
        for same in (False, True):
            world = World(self)
            world.standard()
            tree = world.remote.rev(world.heads[1] + "^{tree}")
            # The other commit is the base itself: a plain push would fast-forward that branch.
            planted = squash_by_hand(world, 1, "fix: change fix/alpha", tree, world.base) if same else world.base
            world.hub.after("GET", REF, lambda world=world, planted=planted: world.remote.set(
                "refs/heads/batch/tested", planted), nth=2)
            message = "push did not create the exact new batch branch" if same else "could not create batch/tested"
            with self.subTest(same=same), self.assertRaisesRegex(MbError, message):
                world.prepare([1])
            self.assertEqual(world.remote.rev("refs/heads/batch/tested"), planted)
            self.assertEqual(world.hub.writes(), [])
            if same:  # the planted commit is exactly the one this run built
                dry = World(self)
                dry.standard()
                self.assertEqual(dry.prepare([1], dry_run=True)["manifest"]["members"][0]["squash_sha"], planted)

    def test_a_change_after_the_push_leaves_the_branch_and_opens_no_pull_request(self):
        world = World(self)
        world.standard()
        world.hub.after("GET", REPO, lambda: world.hub.pull(1, "fix/alpha", title="fix: renamed"), nth=3)
        with self.assertRaisesRegex(MbError, "#1 changed after batch/tested was pushed; delete that branch"):
            world.prepare([1, 2])
        self.assertIsNotNone(world.remote.rev("refs/heads/batch/tested"))
        self.assertEqual(world.hub.writes(), [])

    def test_a_refused_pull_request_is_an_error(self):
        world = World(self)
        world.standard()
        world.hub.add_pull({**world.hub.pull(9, "batch/tested"), "number": 9})
        with self.assertRaisesRegex(ApiError, "already exists"):
            world.prepare([1])

    def test_titles_cannot_forge_a_marker_or_break_the_table(self):
        world = World(self)
        world.standard()
        world.hub.pull(1, "fix/alpha", title='fix: <!-- mod-base-batch {"kind":"x"} --> | pipe & more')
        result = world.prepare([1])
        body = world.hub.mutations[0][2]["body"]
        self.assertEqual(body.count("<!-- mod-base-batch "), 1)
        self.assertIn('| #1 | fix: &lt;!-- mod-base-batch {"kind":"x"} --&gt; \\| pipe &amp; more |', body)
        self.assertEqual(batch.read_batch_marker(body), result["manifest"])
        self.assertEqual(world.remote.git("log", "-1", "--format=%s", "refs/heads/batch/tested"),
                         'fix: <!-- mod-base-batch {"kind":"x"} --> | pipe & more (#1)')

    def test_a_description_beyond_the_body_bound_stops_before_the_push(self):
        world = World(self)
        numbers = tuple(range(1, 18))
        for number in numbers:
            world.member(number, f"fix/n{number}", {**BASE_FILES, f"src/n{number}.txt": b"x\n"},
                         title="\U0001f600" * limits.MAX_CI_BATCH_TITLE_CHARS)
        with self.assertRaisesRegex(MbError, "exceeds the pull request body bound"):
            world.prepare(numbers)
        self.assertEqual(world.hub.writes(), [])
        self.assertIsNone(world.remote.rev("refs/heads/batch/tested"))

    def test_many_members_get_a_short_title(self):
        world = World(self)
        numbers = tuple(range(101, 121))
        for number in numbers:
            world.member(number, f"fix/n{number}", {**BASE_FILES, f"src/n{number}.txt": b"x\n"})
        world.prepare(numbers)
        self.assertEqual(world.hub.mutations[0][2]["title"], "chore: batch 20 pull requests")


@POSIX_ONLY
class RebuildTests(unittest.TestCase):
    """The verifier: a manifest is true only if building its stack again gives exactly it."""

    def published(self) -> tuple[World, dict]:
        world = World(self)
        world.standard()
        return world, world.prepare([1, 2])["manifest"]

    def rebuild(self, world: World, manifest: dict) -> None:
        with world.store() as store:
            batch.rebuild_batch(store, manifest)

    def test_a_published_batch_rebuilds_even_after_its_members_and_base_moved_on(self):
        world, manifest = self.published()
        self.rebuild(world, manifest)
        world.move(1, "fix/alpha", world.remote.commit({**BASE_FILES, "alpha.txt": b"later\n"}, world.base))
        world.remote.set("refs/heads/master", world.remote.commit({**BASE_FILES, "keep.txt": b"k\n"}, world.base))
        before = copy.deepcopy(manifest)
        self.rebuild(world, manifest)
        self.assertEqual(manifest, before)

    def test_a_commit_holding_a_file_outside_its_members_patch_is_rejected(self):
        world, manifest = self.published()
        first, second = manifest["members"]
        # The same stack by hand, with one extra file smuggled into the first commit.
        tree = world.remote.tree({**BASE_FILES, "alpha.txt": b"alpha\n", ".github/workflows/evil.yml": b"on: push\n"})
        forged_first = squash_by_hand(world, 1, first["title"], tree, world.base)
        final = world.remote.tree({**world.remote.files(second["result_tree"]),
                                   ".github/workflows/evil.yml": b"on: push\n"})
        forged_second = squash_by_hand(world, 2, second["title"], final, forged_first)
        forged = copy.deepcopy(manifest)
        forged["members"][0].update(squash_sha=forged_first, result_tree=tree)
        forged["members"][1].update(squash_sha=forged_second, result_tree=final)
        forged["result_tree"] = final
        batch.batch_marker(forged)  # a well-formed claim
        with self.assertRaisesRegex(MbError, "differs from the stack rebuilt from its base and member heads"):
            self.rebuild(world, forged)
        # The honest commits are what the same messages give for the members' own patches.
        self.assertEqual(squash_by_hand(world, 1, first["title"], first["result_tree"], world.base),
                         first["squash_sha"])

    def test_every_edited_field_is_rejected(self):
        world, manifest = self.published()
        other = world.heads[3]
        edits = {
            "title": lambda document: document["members"][0].update(title="fix: another title"),
            "number": lambda document: document["members"][0].update(pr_number=7),
            "head": lambda document: document["members"][0].update(head_sha=other),
            "head tree": lambda document: document["members"][0].update(head_tree="9" * 40),
            "merge base": lambda document: document["members"][1].update(merge_base_sha=world.heads[1]),
            "patch": lambda document: document["members"][1].update(patch_sha256="9" * 64),
            "squash": lambda document: document["members"][0].update(squash_sha="9" * 40),
            "base": lambda document: document.update(base_sha=world.heads[3]),
            "base tree": lambda document: document.update(base_tree="9" * 40),
            "order": lambda document: (document["members"].reverse(),
                                       document.update(result_tree=document["members"][-1]["result_tree"])),
        }
        for label, edit in edits.items():
            document = copy.deepcopy(manifest)
            edit(document)
            with self.subTest(label=label), self.assertRaises(MbError):
                self.rebuild(world, document)
        # A shorter stack is a true batch of its own, and the names Git cannot see are only copied:
        # settlement binds the repository, both branches and the last commit to the pull request.
        document = copy.deepcopy(manifest)
        document["members"].pop()
        document.update(result_tree=document["members"][-1]["result_tree"], branch="batch/other")
        self.rebuild(world, document)

    def test_a_head_the_repository_no_longer_holds_cannot_be_rebuilt(self):
        world, manifest = self.published()
        document = copy.deepcopy(manifest)
        document["members"][0]["head_sha"] = "a" * 40
        with self.assertRaisesRegex(MbError, "git fetch failed"):
            self.rebuild(world, document)
        with self.assertRaises(MbError):
            self.rebuild(world, {**manifest, "approval": True})


if __name__ == "__main__":
    unittest.main()
