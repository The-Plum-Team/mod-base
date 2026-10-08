"""Subject authentication against a fake GitHub, the private identity record and the policy digest."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mod_base import runtime
from mod_base.build_ci import identity
from mod_base.build_ci.config import load_build_config
from mod_base.build_ci.protocol import CALLER_WORKFLOWS, validate_subject
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.pin import kit_tree_digest
from tests import ci_mod_harness as h


def expected_subject(*, pull_request: bool = True) -> dict:
    subject = h.subject(pull_request=pull_request)
    subject["kit"]["tree_digest"] = kit_tree_digest(runtime.kit_root())
    return subject


def invocation(environment: dict[str, str], root: Path = h.MOD) -> runtime.Invocation:
    return runtime.build_invocation(root, None, environment)


def authenticate(api, *, producer: str = "build", pr_number: int | None = 7, environment: dict[str, str] | None = None,
                 root: Path = h.MOD) -> dict:
    environment = h.environment() if environment is None else environment
    return identity.authenticate_subject(invocation(environment, root), api, producer=producer, pr_number=pr_number)


class PullRequestSubjectTests(unittest.TestCase):
    def test_a_ready_pull_request_yields_the_complete_subject_in_seven_requests(self) -> None:
        api, _ = h.github()
        record = authenticate(api)
        self.assertEqual(record, {"producer": "build", "event": "pull_request_target",
                                  "workflow_path": CALLER_WORKFLOWS["build"], "controller_tree": h.CONTROLLER_TREE,
                                  "subject": expected_subject()})
        self.assertEqual(record["subject"]["tested_parents"], [h.CONTROLLER_SHA, h.HEAD_SHA])
        self.assertEqual((record["subject"]["tested_sha"], record["subject"]["tested_tree"]), (h.TESTED_SHA, h.TESTED_TREE))
        self.assertEqual(api.request_count, 7)
        self.assertEqual(api.mutations, [])

    def test_the_packaged_producer_derives_the_same_identity_as_the_build_producer(self) -> None:
        build = authenticate(h.github()[0])
        packaged = authenticate(h.github()[0], producer="packaged", environment=h.environment(caller="packaged"))
        self.assertEqual(packaged["subject"], build["subject"])
        self.assertEqual(packaged["subject"]["controller_workflow"], CALLER_WORKFLOWS["build"])
        self.assertEqual((packaged["producer"], packaged["workflow_path"]), ("packaged", CALLER_WORKFLOWS["packaged"]))

    def test_a_draft_is_a_rejection(self) -> None:
        api, pull = h.github()
        h.seed_pull_request(api, {**pull, "draft": True})
        with self.assertRaises(identity.SubjectError) as caught:
            authenticate(api)
        self.assertEqual(caught.exception.reason, "draft")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_a_pull_request_without_a_test_merge_is_a_rejection(self) -> None:
        api, pull = h.github()
        h.seed_pull_request(api, {**pull, "merge_commit_sha": None})
        with self.assertRaises(identity.SubjectError) as caught:
            authenticate(api)
        self.assertEqual(caught.exception.reason, "no-test-merge")

    def test_the_test_merge_must_have_exactly_base_then_head_as_parents(self) -> None:
        for parents in ([h.HEAD_SHA, h.CONTROLLER_SHA], [h.CONTROLLER_SHA], [h.CONTROLLER_SHA, "9" * 40],
                        ["9" * 40, h.HEAD_SHA], [], [h.CONTROLLER_SHA, h.HEAD_SHA, "9" * 40]):
            api, _ = h.github()
            api.add_commit(h.TESTED_SHA, h.TESTED_TREE, parents=parents)
            with self.subTest(parents=parents), self.assertRaises(MbError) as caught:
                authenticate(api)
            self.assertIn("parents", str(caught.exception))
            self.assertEqual(api.request_count, 7)

    def test_a_malformed_or_foreign_commit_object_is_a_rejection(self) -> None:
        path = f"/repos/{h.REPOSITORY}/git/commits/{h.TESTED_SHA}"
        parents = [{"sha": h.CONTROLLER_SHA}, {"sha": h.HEAD_SHA}]
        for response in ({"sha": "9" * 40, "tree": {"sha": h.TESTED_TREE}, "parents": parents},
                         {"sha": h.TESTED_SHA, "tree": {"sha": "not-a-tree"}, "parents": parents},
                         {"sha": h.TESTED_SHA, "tree": h.TESTED_TREE, "parents": parents},
                         {"sha": h.TESTED_SHA, "tree": {"sha": h.TESTED_TREE}, "parents": [h.CONTROLLER_SHA, h.HEAD_SHA]},
                         {"sha": h.TESTED_SHA, "tree": {"sha": h.TESTED_TREE}},
                         [h.TESTED_SHA]):
            api, _ = h.github()
            api.add_response(path, response)
            with self.subTest(response=response), self.assertRaises(MbError):
                authenticate(api)

    def test_a_moved_base_a_foreign_head_and_a_closed_pull_request_are_rejections(self) -> None:
        def moved(api, pull) -> None:
            api.set_branch(h.BRANCH, "9" * 40, "8" * 40)

        def fork(api, pull) -> None:
            pull["head"]["repo"]["full_name"] = "fork/synthetic-mod"

        def other_base(api, pull) -> None:
            pull["base"]["ref"] = "release"

        def stale_base(api, pull) -> None:
            pull["base"]["sha"] = "9" * 40

        def closed(api, pull) -> None:
            pull["state"] = "closed"

        for change in (moved, fork, other_base, stale_base, closed):
            api, pull = h.github()
            change(api, pull)
            h.seed_pull_request(api, pull)
            with self.subTest(change=change.__name__), self.assertRaises(MbError):
                authenticate(api)

    def test_what_the_environment_claims_is_checked_before_any_request(self) -> None:
        def without(name: str) -> dict[str, str]:
            return {key: value for key, value in h.environment().items() if key != name}

        workflow = "GITHUB_WORKFLOW_REF"
        environments = {
            "push event": h.environment(event="push"),
            "unknown event": h.environment(event="pull_request"),
            "no event": without("GITHUB_EVENT_NAME"),
            "packaged caller": h.environment(caller="packaged"),
            "unmanaged caller": {**h.environment(), workflow: f"{h.REPOSITORY}/.github/workflows/ci.yml@refs/heads/main"},
            "foreign caller": {**h.environment(), workflow: f"other/mod/{CALLER_WORKFLOWS['build']}@refs/heads/main"},
            "tag ref": {**h.environment(), workflow: f"{h.REPOSITORY}/{CALLER_WORKFLOWS['build']}@refs/tags/v1"},
            "no workflow ref": without(workflow),
            "no kit": without("MOD_BASE_KIT_SHA"),
            "no controller": without("GITHUB_SHA"),
            "other repository": {**h.environment(), "GITHUB_REPOSITORY": "example/other"},
        }
        for label, environment in environments.items():
            api, _ = h.github()
            with self.subTest(case=label), self.assertRaises(MbError):
                authenticate(api, environment=environment)
            self.assertEqual(api.request_count, 0, label)
        api, _ = h.github()
        with self.assertRaises(MbError):
            authenticate(api, producer="status")
        self.assertEqual(api.request_count, 0)

    def test_the_run_its_caller_and_the_canonical_branch_must_be_the_default_branch(self) -> None:
        for name, value in (("GITHUB_REF", "refs/heads/feature/synthetic"), ("GITHUB_REF", "refs/pull/7/merge"),
                            ("GITHUB_WORKFLOW_REF", f"{h.REPOSITORY}/{CALLER_WORKFLOWS['build']}@refs/heads/release")):
            with self.subTest(name=name, value=value), self.assertRaises(MbError):
                authenticate(h.github()[0], environment={**h.environment(), name: value})
        with tempfile.TemporaryDirectory() as directory:
            root = h.materialize(Path(directory) / "mod")
            path = root / "site" / "mod-base.json"
            path.write_bytes(h.pretty({**json.loads(path.read_bytes()), "canonical_branch": "release"}))
            with self.assertRaises(MbError):
                authenticate(h.github()[0], root=root)


class ProtectedSubjectTests(unittest.TestCase):
    def test_a_push_to_the_default_branch_is_its_own_controller_in_five_requests(self) -> None:
        for event in identity.PROTECTED_EVENTS:
            api, _ = h.github()
            record = authenticate(api, pr_number=None, environment=h.environment(event=event))
            with self.subTest(event=event):
                self.assertEqual(record, {"producer": "build", "event": event, "workflow_path": CALLER_WORKFLOWS["build"],
                                          "controller_tree": h.CONTROLLER_TREE,
                                          "subject": expected_subject(pull_request=False)})
                subject = record["subject"]
                self.assertEqual({subject["head_sha"], subject["tested_sha"], subject["controller_sha"],
                                  subject["base_sha"]}, {h.CONTROLLER_SHA})
                self.assertEqual((subject["pr_number"], subject["tested_parents"]), (0, ["b" * 40]))
                self.assertEqual(api.request_count, 5)

    def test_build_jobs_also_run_inside_the_packaged_caller_but_never_for_a_pull_request(self) -> None:
        environment = h.environment(event="workflow_dispatch", caller="packaged")
        rebuilt = authenticate(h.github()[0], pr_number=None, environment=environment)
        self.assertEqual((rebuilt["producer"], rebuilt["workflow_path"]), ("build", CALLER_WORKFLOWS["packaged"]))
        self.assertEqual(rebuilt["subject"], expected_subject(pull_request=False))
        with self.assertRaises(MbError):
            authenticate(h.github()[0], environment=h.environment(caller="packaged"))
        with self.assertRaises(MbError):
            authenticate(h.github()[0], producer="packaged", pr_number=None, environment=h.environment(event="push"))

    def test_a_pull_request_number_and_a_protected_event_never_mix(self) -> None:
        for event, number in (("push", 7), ("schedule", 7), ("pull_request_target", None)):
            api, _ = h.github()
            with self.subTest(event=event, number=number), self.assertRaises(MbError):
                authenticate(api, pr_number=number, environment=h.environment(event=event))
            self.assertEqual(api.request_count, 0)

    def test_a_default_branch_that_moved_is_a_rejection(self) -> None:
        api, _ = h.github()
        api.set_branch(h.BRANCH, "9" * 40, "8" * 40)
        with self.assertRaises(identity.SubjectError) as caught:
            authenticate(api, pr_number=None, environment=h.environment(event="push"))
        self.assertEqual(caught.exception.reason, "controller-moved")

    def test_the_default_branch_is_read_again_before_the_subject_is_returned(self) -> None:
        api, _ = h.github()
        api.during_listing(f"/repos/{h.REPOSITORY}/git/commits/{h.CONTROLLER_SHA}",
                           lambda: api.set_branch(h.BRANCH, "9" * 40, "8" * 40))
        with self.assertRaises(MbError) as caught:
            authenticate(api, pr_number=None, environment=h.environment(event="push"))
        self.assertIn("moved during authentication", str(caught.exception))

    def test_the_branch_tree_and_the_commit_object_must_agree(self) -> None:
        api, _ = h.github()
        api.add_response(f"/repos/{h.REPOSITORY}/git/commits/{h.CONTROLLER_SHA}",
                         {"sha": h.CONTROLLER_SHA, "tree": {"sha": "8" * 40}, "parents": [{"sha": "b" * 40}]})
        with self.assertRaises(MbError):
            authenticate(api, pr_number=None, environment=h.environment(event="push"))


class RecordTests(unittest.TestCase):
    def record(self, *, pull_request: bool = True) -> dict:
        return {"producer": "build", "event": "pull_request_target" if pull_request else "push",
                "workflow_path": CALLER_WORKFLOWS["build"],
                "controller_tree": h.CONTROLLER_TREE, "subject": h.subject(pull_request=pull_request)}

    def test_both_kinds_of_subject_are_valid_records(self) -> None:
        for pull_request in (True, False):
            record = self.record(pull_request=pull_request)
            self.assertIs(identity.validate_subject_record(record), record)
            validate_subject(record["subject"])

    def test_hostile_records_are_rejected(self) -> None:
        mutations = [
            lambda r: r.update(extra=1),
            lambda r: r.pop("producer"),
            lambda r: r.update(producer="status"),
            lambda r: r.update(event="pull_request"),
            lambda r: r.update(event="push"),
            lambda r: r.update(workflow_path=CALLER_WORKFLOWS["packaged"]),
            lambda r: r.update(workflow_path=".github/workflows/ci.yml"),
            lambda r: r.update(controller_tree="tree"),
            lambda r: r["subject"].update(controller_workflow=CALLER_WORKFLOWS["packaged"]),
            lambda r: r["subject"].update(policy_sha256="0" * 64),
            lambda r: r["subject"].pop("kit"),
            lambda r: r["subject"].update(pr_number=True),
            lambda r: r["subject"].update(tested_parents=[h.HEAD_SHA, h.CONTROLLER_SHA]),
            lambda r: r["subject"].update(tested_sha=h.HEAD_SHA),
            lambda r: r["subject"].update(graph_version=2),
            lambda r: r["subject"]["kit"].update(repository="example/kit"),
        ]
        for index, mutate in enumerate(mutations):
            record = self.record()
            mutate(record)
            with self.subTest(index=index), self.assertRaises(MbError):
                identity.validate_subject_record(record)
        protected = self.record(pull_request=False)
        protected["controller_tree"] = "8" * 40
        with self.assertRaises(MbError):
            identity.validate_subject_record(protected)


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name)
        self.state = self.parent / "state"
        self.record = RecordTests().record()

    def test_the_record_is_private_canonical_and_read_back_strictly(self) -> None:
        identity.write_subject(self.state, self.record)
        self.assertEqual(os.listdir(self.state), [identity.IDENTITY_NAME])
        self.assertEqual(self.state.stat().st_mode & 0o7777, 0o700)
        path = self.state / identity.IDENTITY_NAME
        self.assertEqual((path.stat().st_mode & 0o7777, path.stat().st_nlink), (0o600, 1))
        self.assertEqual(path.read_bytes(), canonical_json(self.record))
        self.assertEqual(identity.read_subject(self.state), self.record)

    def test_an_existing_state_is_never_adopted_or_replaced(self) -> None:
        identity.write_subject(self.state, self.record)
        before = (self.state / identity.IDENTITY_NAME).read_bytes()
        other = copy.deepcopy(self.record)
        other["subject"]["head_branch"] = "feature/other"
        for write in (lambda: identity.write_subject(self.state, other), lambda: identity.create_state(self.state),
                      lambda: identity.write_state_record(self.state, identity.IDENTITY_NAME, b"{}\n")):
            with self.assertRaises(identity.StateError):
                write()
        self.assertEqual((self.state / identity.IDENTITY_NAME).read_bytes(), before)
        empty = self.parent / "empty"
        empty.mkdir(mode=0o700)
        with self.assertRaises(identity.StateError):
            identity.write_subject(empty, self.record)
        self.assertEqual(os.listdir(empty), [])

    def test_an_invalid_record_writes_nothing(self) -> None:
        self.record["subject"]["tested_parents"] = []
        with self.assertRaises(MbError):
            identity.write_subject(self.state, self.record)
        self.assertFalse(self.state.exists())

    def test_a_state_that_is_not_private_is_refused(self) -> None:
        def group_directory() -> None:
            self.state.chmod(0o750)

        def world_file() -> None:
            (self.state / identity.IDENTITY_NAME).chmod(0o644)

        def linked_file() -> None:
            os.link(self.state / identity.IDENTITY_NAME, self.parent / "second-name")

        def symlinked_file() -> None:
            path = self.state / identity.IDENTITY_NAME
            path.rename(self.parent / "moved.json")
            path.symlink_to(self.parent / "moved.json")

        def symlinked_directory() -> None:
            self.state.rename(self.parent / "real-state")
            self.state.symlink_to(self.parent / "real-state", target_is_directory=True)

        def missing_file() -> None:
            (self.state / identity.IDENTITY_NAME).unlink()

        def directory_instead() -> None:
            (self.state / identity.IDENTITY_NAME).unlink()
            (self.state / identity.IDENTITY_NAME).mkdir()

        for index, damage in enumerate((group_directory, world_file, linked_file, symlinked_file, symlinked_directory,
                                        missing_file, directory_instead)):
            self.state = self.parent / f"state-{index}"
            identity.write_subject(self.state, self.record)
            identity.read_subject(self.state)
            damage()
            with self.subTest(damage=damage.__name__), self.assertRaises(identity.StateError):
                identity.read_subject(self.state)

    def test_bytes_that_are_not_the_canonical_valid_record_are_refused(self) -> None:
        raw = canonical_json(self.record)
        changed = copy.deepcopy(self.record)
        changed["subject"]["tested_sha"] = h.HEAD_SHA
        for index, data in enumerate((json.dumps(self.record).encode("utf-8"), raw.rstrip(b"\n"), raw + b"\n",
                                      raw.replace(b'"producer"', b'"producer":"build","producer"', 1),
                                      canonical_json(changed), canonical_json({**self.record, "authority": "success"}),
                                      b"\xef\xbb\xbf" + raw, b" " * (limits.MAX_CI_IDENTITY_BYTES + 1))):
            state = self.parent / f"bytes-{index}"
            identity.create_state(state)
            identity.write_state_record(state, identity.IDENTITY_NAME, data)
            with self.subTest(index=index), self.assertRaises(MbError):
                identity.read_subject(state)

    def test_record_names_are_single_plain_components(self) -> None:
        identity.create_state(self.state)
        for name in ("../escape.json", "sub/record.json", "", ".", "..", "/abs.json", "a b.json"):
            with self.subTest(name=name), self.assertRaises(identity.StateError):
                identity.write_state_record(self.state, name, b"x")
            with self.subTest(name=name), self.assertRaises(identity.StateError):
                identity.read_state_record(self.state, name, max_bytes=16)
        self.assertEqual(os.listdir(self.state), [])
        self.assertEqual(os.listdir(self.parent), ["state"])
        identity.write_state_record(self.state, "ci-plan.json", b"plan")
        self.assertEqual(identity.read_state_record(self.state, "ci-plan.json", max_bytes=4), b"plan")
        with self.assertRaises(identity.StateError):
            identity.read_state_record(self.state, "ci-plan.json", max_bytes=3)


class PolicyDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_build_config(h.MOD, repository=h.REPOSITORY)
        self.subject = h.subject()

    def test_the_digest_is_the_documented_closed_formula(self) -> None:
        self.assertEqual(identity.policy_sha256(self.config, self.subject), canonical_sha256({
            "format": "mod-base.build.policy-v1", "build_adapter_api": 1, "graph_versions": {"build": 1, "packaged": 1},
            "kit": self.subject["kit"], "config_sha256": self.config.sha256,
            "adapter_files": [{"path": file.path, "sha256": file.sha256} for file in self.config.files]}))
        self.assertEqual(len(self.config.files), 3)

    def test_every_control_plane_change_alters_it(self) -> None:
        digest = identity.policy_sha256(self.config, self.subject)
        with tempfile.TemporaryDirectory() as directory:
            changed = []
            for index, edit in enumerate(("config", "adapter", "dispatcher", "policy")):
                root = h.materialize(Path(directory) / str(index))
                document = json.loads((root / "scripts/ci/mod-base-build.json").read_bytes())
                if edit == "config":
                    document["timeouts"]["target_seconds"] += 1
                else:
                    entry = document["adapter"]["files"][("adapter", "dispatcher", "policy").index(edit)]
                    source = root / entry["path"]
                    source.write_bytes(source.read_bytes() + b"# reviewed change\n")
                    entry["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                (root / "scripts/ci/mod-base-build.json").write_bytes(h.pretty(document))
                changed.append(identity.policy_sha256(load_build_config(root, repository=h.REPOSITORY), self.subject))
            reformatted = h.materialize(Path(directory) / "reformatted")
            path = reformatted / "scripts/ci/mod-base-build.json"
            path.write_bytes(json.dumps(json.loads(path.read_bytes())).encode("utf-8"))
            changed.append(identity.policy_sha256(load_build_config(reformatted, repository=h.REPOSITORY), self.subject))
        for key, value in (("sha", "9" * 40), ("version", "9.9.9"), ("tree_digest", "sha256:" + "9" * 64)):
            subject = copy.deepcopy(self.subject)
            subject["kit"][key] = value
            changed.append(identity.policy_sha256(self.config, subject))
        self.assertEqual(len({digest, *changed}), len(changed) + 1)

    def test_nothing_else_of_the_subject_alters_it(self) -> None:
        digest = identity.policy_sha256(self.config, self.subject)
        self.assertEqual(identity.policy_sha256(self.config, h.subject(pull_request=False)), digest)
        moved = copy.deepcopy(self.subject)
        moved.update(head_sha="9" * 40, tested_sha="8" * 40, tested_tree="7" * 40,
                     tested_parents=[h.CONTROLLER_SHA, "9" * 40], head_branch="feature/other", pr_number=8)
        self.assertEqual(identity.policy_sha256(self.config, moved), digest)

    def test_foreign_or_unverified_configs_are_rejected(self) -> None:
        with self.assertRaises(MbError):
            identity.policy_sha256(self.config, {**self.subject, "repository": "example/other",
                                                 "source_repository": "example/other"})
        with self.assertRaises(MbError):
            identity.policy_sha256(self.config.data, self.subject)
        with self.assertRaises(MbError):
            identity.policy_sha256(self.config, {**self.subject, "policy_sha256": "0" * 64})


if __name__ == "__main__":
    unittest.main()
