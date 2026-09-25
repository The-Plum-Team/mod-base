"""``targets`` (MB5): enrolled-branch listing, inert fetches and adapter target validation.

Ports Block Pops ``test_pages_branch_discovery`` / ``test_pages_discovery_inventory`` to v1: the
inventory is the branches API (one bounded page, exact commit and tree identities), heads are
fetched only as inert objects (never checked out, https-only, no credentials, no ambient ``GIT_*``
redirection, no replacement objects), enrollment is the adapter's decision over those objects
(copied, unreadable or missing matrices are simply not enrolled), and any inconsistency fails the
whole inventory instead of falling back to a partial one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.pages import targets
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH
from tests.fixtures.mods import support

RELEASE = "release/1.21.1"


def token(branch: str) -> str:
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]


def git_environment(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


class Repositories(unittest.TestCase):
    """A bp-like ``origin`` with several branches and a ``local`` clone of ``master`` only."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-targets-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.origin = support.materialize("bp_like", self.directory / "origin")
        matrix = json.loads((self.origin.root / "release/release-matrix.json").read_text())
        release = dict(matrix, branch={**matrix["branch"], "name": RELEASE})
        self.heads = {
            "master": self.origin,
            RELEASE: support.commit_on_branch(self.origin, RELEASE, {
                "release/release-matrix.json": json.dumps(release).encode()}),
            "feature/copied": support.commit_on_branch(self.origin, "feature/copied", {"notes.txt": b"copied\n"}),
            "feature/unreadable": support.commit_on_branch(self.origin, "feature/unreadable", {
                "release/release-matrix.json": b"not JSON"}),
            "feature/no-matrix": support.commit_on_branch(self.origin, "feature/no-matrix", {
                "release/release-matrix.json": None}),
        }
        self.local = self.directory / "local"
        subprocess.run(["git", "clone", "-q", "--no-local", "--single-branch", "--branch", "master",
                        str(self.origin.root), str(self.local)], check=True, capture_output=True,
                       env=git_environment(self.directory), timeout=60)
        self.api = FakeGitHub(repository=self.origin.repository)
        for name, head in self.heads.items():
            self.api.set_branch(name, head.commit, head.tree)
        self.host = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)
        protocols = mock.patch.object(targets, "FETCH_PROTOCOLS", ("https", "file"))
        protocols.start()
        self.addCleanup(protocols.stop)
        sleeper = mock.patch.object(targets, "_sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def invocation(self, *, config: Path | None = None, sha: str | None = None):
        mod = support.FixtureMod("bp_like", self.local, self.origin.commit, self.origin.tree, "master",
                                 self.origin.repository)
        environ = support.environment(mod, run_id=9000, job="admit", sha=sha, workflow=PAGES_WORKFLOW_PATH,
                                      token="fixture-token")
        return build_invocation(self.local, config, environ)

    def config_with(self, **changes: Any) -> Path:
        data = json.loads((self.local / "site/mod-base.json").read_text())
        data["targets"].update(changes)
        path = self.directory / "config.json"
        path.write_text(json.dumps(data))
        return path

    def has(self, commit: str) -> bool:
        return subprocess.run(["git", "-C", str(self.local), "cat-file", "-e", f"{commit}^{{commit}}"],
                              capture_output=True, env=git_environment(self.directory), check=False).returncode == 0


class EnrolledBranchesTest(Repositories):
    def test_listing_binds_exact_commits_and_trees_from_one_page(self) -> None:
        listed = targets.list_enrolled_branches(self.api, max_branches=100)
        self.assertEqual([row["name"] for row in listed], sorted(self.heads))
        for row in listed:
            head = self.heads[row["name"]]
            self.assertEqual((row["commit"], row["tree"]), (head.commit, head.tree))
        self.assertEqual(self.api.request_count, 1 + len(self.heads), "one listing page plus one commit read each")

    def test_only_self_identifying_branches_are_enrolled_from_inert_objects(self) -> None:
        self.assertFalse(self.has(self.heads[RELEASE].commit))
        (self.local / "release/release-matrix.json").write_bytes(b"uncommitted broken worktree")
        found = targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual({target["key"]: target["subject"] for target in found},
                         {token("master"): self.heads["master"].subject, token(RELEASE): self.heads[RELEASE].subject})
        self.assertEqual([target["label"] for target in found], ["master", RELEASE])
        self.assertTrue(self.has(self.heads[RELEASE].commit), "the release head was fetched as an inert object")
        self.assertEqual(subprocess.run(["git", "-C", str(self.local), "rev-parse", "HEAD"], capture_output=True,
                                        env=git_environment(self.directory), check=True).stdout.decode().strip(),
                         self.origin.commit, "nothing was checked out")
        self.assertEqual(self.host.calls, [("targets", False)])

    def test_canonical_absence_and_movement_invalidate_the_whole_inventory(self) -> None:
        api = FakeGitHub(repository=self.origin.repository)
        api.set_branch(RELEASE, self.heads[RELEASE].commit, self.heads[RELEASE].tree)
        with self.assertRaisesRegex(MbError, "not in the branch listing"):
            targets.discover_targets(self.invocation(), api=api)
        self.api.set_branch("master", self.heads[RELEASE].commit, self.heads[RELEASE].tree)
        with self.assertRaises(MbError) as caught:
            targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual(caught.exception.reason, "stale-implementation")
        self.assertEqual(self.host.calls, [])

    def test_trees_come_from_the_fetched_objects_in_one_api_read(self) -> None:
        # The tree of a content-addressed commit object is what git verified on fetch; reading it
        # per branch from the API as well would make the admission budget grow with the branches.
        release = self.heads[RELEASE]
        self.api.add_response(f"/repos/{self.origin.repository}/git/commits/{release.commit}",
                              {"sha": release.commit, "tree": {"sha": self.origin.tree}})
        found = targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual({target["key"]: target["subject"]["tree"] for target in found},
                         {token("master"): self.origin.tree, token(RELEASE): release.tree})
        self.assertEqual(self.api.request_count, 1, "only the branch listing is read")

    def test_branches_sharing_a_head_are_fetched_once(self) -> None:
        self.api.set_branch("release/next", self.heads[RELEASE].commit, self.heads[RELEASE].tree)
        self.api.set_branch("master-copy", self.origin.commit, self.origin.tree)
        with mock.patch.object(targets, "fetch_inert", wraps=targets.fetch_inert) as fetch:
            found = targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual(sorted(fetch.call_args.args[1]), sorted({head.commit for head in self.heads.values()}))
        self.assertEqual({target["label"] for target in found}, {"master", RELEASE},
                         "a copied matrix does not enroll its other name")
        self.assertEqual(self.host.calls, [("targets", False)])

    def test_inventory_limits_fail_closed(self) -> None:
        with self.assertRaises(MbError) as caught:
            targets.discover_targets(self.invocation(config=self.config_with(max_branches=len(self.heads) - 1)),
                                     api=self.api)
        self.assertEqual(caught.exception.reason, "branch-limit")
        for index in range(100 - len(self.heads)):
            self.api.set_branch(f"topic/{index:03}", self.origin.commit, self.origin.tree)
        with self.assertRaises(MbError) as caught:
            targets.list_enrolled_branches(self.api, max_branches=100)
        self.assertEqual(caught.exception.reason, "branch-limit")
        for value in (0, 101, True, "5"):
            with self.subTest(value=value), self.assertRaises(MbError):
                targets.list_enrolled_branches(self.api, max_branches=value)

    def test_malformed_listing_rows_fail_closed(self) -> None:
        path = f"/repos/{self.origin.repository}/branches"
        valid = {"name": "master", "commit": {"sha": self.origin.commit}}
        for rows in ([valid, valid], [{"name": "master"}], [{"name": "master", "commit": {"sha": "A" * 40}}],
                     ["master"], {"branches": [valid]}, [valid, {"name": 7, "commit": {"sha": self.origin.commit}}],
                     [valid, {"name": "", "commit": {"sha": self.origin.commit}}],
                     [valid, {"name": "deps+bump", "commit": {"sha": "x"}}],
                     [{"name": "a" * 201, "commit": {"sha": self.origin.commit}}] * 2):
            api = FakeGitHub(repository=self.origin.repository)
            api.add_response(path, rows, params={"per_page": 100, "page": 1})
            with self.subTest(rows=str(rows)[:80]), self.assertRaises(MbError):
                targets.list_enrolled_branches(api, max_branches=100)

    def test_names_that_can_never_be_subjects_are_skipped_not_fatal(self) -> None:
        # Block Pops accepted every valid Git branch name; a pushed ``deps+bump`` branch must not
        # stop publication for every enrolled branch.
        path = f"/repos/{self.origin.repository}/branches"
        odd = ["../unsafe", "deps+bump", "fix/issue#12", "feature/\u00fcber", "renovate/@types-node", "a" * 201]
        rows = [{"name": name, "commit": {"sha": self.origin.commit}} for name in odd]
        rows.append({"name": "master", "commit": {"sha": self.origin.commit}})
        self.api.add_response(path, rows, params={"per_page": 100, "page": 1})
        self.assertEqual(targets.branch_heads(self.api, max_branches=100), {"master": self.origin.commit})
        found = targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual([target["label"] for target in found], ["master"])
        with self.assertRaises(MbError) as caught:
            targets.branch_heads(self.api, max_branches=len(odd))
        self.assertEqual(caught.exception.reason, "branch-limit", "skipped names still count toward the bound")

    def test_enrolled_mode_requires_the_api(self) -> None:
        with self.assertRaisesRegex(MbError, "API"):
            targets.discover_targets(self.invocation(), api=None)

    def test_ambient_git_redirection_and_replacements_cannot_redefine_a_head(self) -> None:
        foreign = support.materialize("qs_like", self.directory / "foreign")
        release = self.heads[RELEASE]
        with mock.patch.dict(os.environ, {"GIT_DIR": str(foreign.root / ".git"), "GIT_WORK_TREE": str(foreign.root),
                                          "GIT_OBJECT_DIRECTORY": str(foreign.root / ".git/objects"),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
                                          "GIT_CONFIG_VALUE_0": "true"}):
            found = targets.discover_targets(self.invocation(), api=self.api)
        self.assertEqual(len(found), 2)
        subprocess.run(["git", "-C", str(self.local), "replace", "-f", self.origin.commit, release.commit],
                       check=True, capture_output=True, env=git_environment(self.directory))
        self.assertEqual(targets.commit_tree_local(self.local, self.origin.commit), self.origin.tree)


class FetchInertTest(Repositories):
    def test_present_commits_are_not_fetched(self) -> None:
        with mock.patch.object(targets.subprocess, "run", wraps=subprocess.run) as calls:
            targets.fetch_inert(self.local, [self.origin.commit])
        self.assertTrue(all("fetch" not in call.args[0] for call in calls.call_args_list))

    def test_a_missing_commit_is_fetched_anonymously_without_ambient_configuration(self) -> None:
        release = self.heads[RELEASE].commit
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GH_TOKEN": "secret"}), \
                mock.patch.object(targets.subprocess, "run", wraps=subprocess.run) as calls:
            targets.fetch_inert(self.local, [self.origin.commit, release], depth=2)
        self.assertTrue(self.has(release))
        fetches = [call for call in calls.call_args_list if "fetch" in call.args[0]]
        self.assertEqual(len(fetches), 1)
        argv, environment = fetches[0].args[0], fetches[0].kwargs["env"]
        for option in ("protocol.version=2", "protocol.allow=never", "credential.helper=", "http.extraHeader=",
                       f"core.hooksPath={os.devnull}"):
            self.assertIn(option, argv)
        self.assertEqual(argv[-3:], ["--depth=2", "origin", release], "only the missing commit is fetched")
        self.assertIn("--no-tags", argv)
        self.assertIn("--no-replace-objects", argv)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertEqual({name for name in environment if name.startswith("GIT_")},
                         {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS",
                          "GIT_NO_LAZY_FETCH"})

    def test_credential_bearing_repository_configuration_refuses_the_fetch(self) -> None:
        # URL-scoped keys outrank the generic ``-c`` resets (git urlmatch prefers the most specific
        # URL), so the kit refuses rather than risk an authenticated fetch.
        release = self.heads[RELEASE].commit
        cases = (("http.https://github.com/.extraheader", "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46c2VjcmV0"),
                 ("http.extraheader", "AUTHORIZATION: bearer secret"),
                 ("credential.https://github.com.helper", "store"),
                 ("credential.helper", "store"),
                 ("url.https://x-access-token:secret@github.com/.insteadOf", "https://github.com/"),
                 ("http.https://github.com/.cookieFile", "/tmp/cookies"),
                 ("core.askPass", "/bin/echo"))
        for key, value in cases:
            with self.subTest(key=key):
                subprocess.run(["git", "-C", str(self.local), "config", key, value], check=True, capture_output=True,
                               env=git_environment(self.directory))
                with self.assertRaises(MbError) as caught:
                    targets.fetch_inert(self.local, [release])
                self.assertEqual(caught.exception.reason, "git-credentials")
                self.assertNotIn("secret", str(caught.exception))
                subprocess.run(["git", "-C", str(self.local), "config", "--unset-all", key], check=True,
                               capture_output=True, env=git_environment(self.directory))
        included = self.directory / "included.gitconfig"
        included.write_text('[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic x\n')
        subprocess.run(["git", "-C", str(self.local), "config", "include.path", str(included)], check=True,
                       capture_output=True, env=git_environment(self.directory))
        with self.assertRaises(MbError) as caught:
            targets.fetch_inert(self.local, [release])
        self.assertEqual(caught.exception.reason, "git-credentials", "included configuration counts too")
        subprocess.run(["git", "-C", str(self.local), "config", "--unset-all", "include.path"], check=True,
                       capture_output=True, env=git_environment(self.directory))
        subprocess.run(["git", "-C", str(self.local), "remote", "set-url", "origin",
                        "https://x-access-token:secret@github.com/The-Plum-Team/bp-like.git"], check=True,
                       capture_output=True, env=git_environment(self.directory))
        with self.assertRaises(MbError) as caught:
            targets.fetch_inert(self.local, [release])
        self.assertEqual(caught.exception.reason, "git-credentials")
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(self.has(release))

    def test_only_https_is_allowed_by_default_and_failures_retry_boundedly(self) -> None:
        release = self.heads[RELEASE].commit
        with mock.patch.object(targets, "FETCH_PROTOCOLS", ("https",)), self.assertRaises(MbError) as caught:
            targets.fetch_inert(self.local, [release])
        self.assertEqual(caught.exception.reason, "git")
        self.assertEqual(self.sleep.call_count, targets.FETCH_ATTEMPTS - 1)
        self.assertFalse(self.has(release))

    def test_hostile_arguments_fail_closed(self) -> None:
        for commits in ("a" * 40, ["A" * 40], [self.origin.commit, self.origin.commit], ["a" * 40] * 101, [1]):
            with self.subTest(commits=str(commits)[:60]), self.assertRaises(MbError):
                targets.fetch_inert(self.local, commits)
        for depth in (0, True, 1001):
            with self.subTest(depth=depth), self.assertRaises(MbError):
                targets.fetch_inert(self.local, [self.origin.commit], depth=depth)
        link = self.directory / "link"
        link.symlink_to(self.local)
        for root in (link, self.directory / "absent"):
            with self.subTest(root=root.name), self.assertRaises(MbError):
                targets.fetch_inert(root, [self.origin.commit])


class DefaultBranchTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-targets-default-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.mod = support.materialize("qs_like", self.directory / "repo")
        environ = support.environment(self.mod, run_id=9000, job="admit", workflow=PAGES_WORKFLOW_PATH)
        self.invocation = build_invocation(self.mod.root, None, environ)
        self.host = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_key_names_the_protected_head(self) -> None:
        found = targets.discover_targets(self.invocation, api=None)
        self.assertEqual([target["key"] for target in found], list(support.QS_KEYS))
        self.assertEqual({json.dumps(target["subject"], sort_keys=True) for target in found},
                         {json.dumps(self.mod.subject, sort_keys=True)})

    def test_another_subject_or_too_many_targets_fail_closed(self) -> None:
        good = targets.discover_targets(self.invocation, api=None)
        for label, crafted in (("branch", [{**good[0], "subject": {**good[0]["subject"], "branch": "topic"}}]),
                               ("tree", [{**good[0], "subject": {**good[0]["subject"], "tree": "e" * 40}}])):
            with self.subTest(label), mock.patch.object(host, "call", return_value=crafted), \
                    self.assertRaisesRegex(MbError, "protected head"):
                targets.discover_targets(self.invocation, api=None)
        data = json.loads((self.mod.root / "site/mod-base.json").read_text())
        data["targets"]["max"] = 1
        config = self.directory / "config.json"
        config.write_text(json.dumps(data))
        limited = build_invocation(self.mod.root, config, dict(self.invocation.environ))
        with self.assertRaisesRegex(MbError, "targets.max"):
            targets.discover_targets(limited, api=None)


if __name__ == "__main__":
    unittest.main()
