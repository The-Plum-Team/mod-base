"""Protected Git import-source admission without importing candidate modules."""

import copy
import hashlib
import tempfile
import unittest
import stat
from contextlib import ExitStack
from pathlib import Path, PurePath
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import adapter, controller
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.protocol import subject_of
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult
from mod_base.build_ci.toolchain import ToolTreeProof
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_config
from tests.test_ci_protocol import protected_subject, seeded_pr


class ControllerSourceTests(unittest.TestCase):
    def test_historical_subject_reads_current_controller_sources(self):
        plan, api, _, protected = self.fixture()
        identity = protected_subject(plan, api)
        sources = controller.authenticate_controller_sources(api, identity=identity, protected_paths=protected)
        self.assertEqual(sources.controller_sha, identity["controller_sha"])
        self.assertNotEqual(sources.controller_sha, identity["tested_sha"])
        self.assertEqual(sources.controller_tree, "8" * 40)
        self.assertEqual(api.mutations, [])

    def fixture(self, *, alter=None, empty_module=False, source_data=None):
        plan, api, pr = seeded_pr()
        config = ci_config()
        rows = []
        data = source_data if source_data is not None else b"raise AssertionError('source must never be imported during admission')\n"
        for file in config["adapter"]["files"]:
            file["sha256"] = hashlib.sha256(data).hexdigest()
            rows.append({"path": file["path"], "mode": "100644", "type": "blob",
                         "sha": api.add_blob(data), "size": len(data)})
        if empty_module:
            name = "scripts/ci/__init__.py"
            config["adapter"]["files"].append({"path": name, "sha256": hashlib.sha256(b"").hexdigest()})
            config["adapter"]["files"].sort(key=lambda file: file["path"])
            rows.append({"path": name, "mode": "100644", "type": "blob", "sha": api.add_blob(b""), "size": 0})
        if alter:
            alter(config, rows)
        raw = canonical_json(config)
        rows.append({"path": controller.BUILD_CONFIG_PATH, "mode": "100644", "type": "blob",
                     "sha": api.add_blob(raw), "size": len(raw)})
        directories = {"/".join(row["path"].split("/")[:index])
                       for row in rows for index in range(1, len(row["path"].split("/")))}
        rows.extend({"path": path, "mode": "040000", "type": "tree", "sha": "9" * 40}
                    for path in sorted(directories))
        api.add_tree("8" * 40, rows)
        protected = tuple(sorted([controller.BUILD_CONFIG_PATH, *[file["path"] for file in config["adapter"]["files"]]]))
        return plan, api, pr, protected

    def test_immutable_controller_blobs_are_bound_to_config_without_import_or_mutation(self):
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        self.assertEqual(sources.controller_sha, plan["identity"]["controller_sha"])
        self.assertEqual(sources.controller_tree, "8" * 40)
        self.assertEqual(len(sources.files), 3)
        self.assertTrue(all(file.data.startswith(b"raise AssertionError") for file in sources.files))
        self.assertEqual(api.mutations, [])

    def test_invalid_policy_paths_and_unprotected_declared_sources_reject(self):
        for changed in ((), [], ("../unsafe",), (controller.BUILD_CONFIG_PATH,) * 2):
            plan, api, _, _ = self.fixture()
            with self.subTest(paths=changed), self.assertRaises(MbError):
                controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=changed)
        plan, api, _, _ = self.fixture()
        with self.assertRaises(MbError):
            controller.authenticate_controller_sources(api, identity=plan["identity"],
                                                       protected_paths=(controller.BUILD_CONFIG_PATH,))

    def test_links_missing_sizes_configured_hash_and_foreign_config_reject(self):
        changes = [lambda config, rows: rows[0].update(mode="120000"),
                   lambda config, rows: rows[0].pop("size"),
                   lambda config, rows: config["adapter"]["files"][0].update(sha256="a" * 64),
                   lambda config, rows: config.update(repository="foreign/mod")]
        for change in changes:
            plan, api, _, protected = self.fixture(alter=change)
            with self.subTest(change=change), self.assertRaises(MbError):
                controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)

    def test_file_and_whole_import_budgets_reject_before_source_blob_downloads(self):
        for bound in ("MAX_CI_ADAPTER_FILE_BYTES", "MAX_CI_ADAPTER_TREE_BYTES"):
            plan, api, _, protected = self.fixture()
            original = controller.blob
            with patch.object(limits, bound, 1), patch.object(controller, "blob", wraps=original) as reading, self.assertRaises(MbError):
                controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
            self.assertEqual(reading.call_count, 1)  # Only the bounded protected config was decoded.

    def test_pr_head_movement_during_download_forbids_return(self):
        plan, api, pr, protected = self.fixture()
        original = controller.blob
        def reading(*args, **kwargs):
            result = original(*args, **kwargs)
            pr["head"]["sha"] = "e" * 40
            api.add_response(f"/repos/{api.repository}/pulls/7", pr)
            return result
        with patch.object(controller, "blob", side_effect=reading), self.assertRaises(MbError):
            controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)

    def test_protected_default_movement_during_download_forbids_return(self):
        plan, api, _, protected = self.fixture()
        original = controller.blob
        def reading(*args, **kwargs):
            result = original(*args, **kwargs)
            api.set_branch(plan["identity"]["base_branch"], "e" * 40, tree="9" * 40)
            return result
        with patch.object(controller, "blob", side_effect=reading), self.assertRaises(MbError):
            controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)

    def test_missing_or_linked_controller_ancestor_rejects_before_blob_reads(self):
        for linked in (False, True):
            plan, api, _, protected = self.fixture()
            listing = api.get_json(f"/repos/{api.repository}/git/trees/{'8' * 40}", params={"recursive": 1})
            rows = listing["tree"]
            if linked:
                next(row for row in rows if row["path"] == "scripts").update(mode="120000", type="blob", size=0)
            else:
                rows[:] = [row for row in rows if row["path"] != "scripts"]
            api.add_tree("8" * 40, rows)
            with self.subTest(linked=linked), patch.object(controller, "blob") as reading, self.assertRaises(MbError):
                controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
            reading.assert_not_called()

    def test_receipt_and_copy_mode_blob_and_sha_are_rechecked_without_imports(self):
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        files = (sources.config, *sources.files)
        records = [{"path": file.path, "mode": file.mode, "size": len(file.data),
                    "git_blob": file.git_blob, "sha256": file.sha256} for file in sorted(files, key=lambda file: file.path)]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(controller, "verify_source_copy", return_value=records) as verify:
                self.assertEqual(controller.verify_controller_source_copy(Path(directory), sources=sources,
                                                                          identity=plan["identity"]), ci_config_with(sources))
                self.assertEqual(len(verify.call_args.kwargs["inventory"]), 4)
            altered = copy.deepcopy(records)
            altered[0]["sha256"] = "a" * 64
            with patch.object(controller, "verify_source_copy", return_value=altered), self.assertRaises(MbError):
                controller.verify_controller_source_copy(Path(directory), sources=sources, identity=plan["identity"])
            (Path(directory) / ".git").write_bytes(b"inert forbidden metadata")
            with self.assertRaises(MbError):
                controller.verify_controller_source_copy(Path(directory), sources=sources, identity=plan["identity"])

    def test_pure_fixed_layout_root_is_inspected_like_a_concrete_copy(self):
        # The handoff passes CONTROLLER_VALIDATION_ROOT, a pure path, straight to these checks.
        self.assertNotIsInstance(controller.CONTROLLER_VALIDATION_ROOT, Path)
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        records = [{"path": file.path, "mode": file.mode, "size": len(file.data),
                    "git_blob": file.git_blob, "sha256": file.sha256}
                   for file in sorted((sources.config, *sources.files), key=lambda file: file.path)]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "verify_source_copy", return_value=records) as verify:
            root = PurePath(directory)
            self.assertEqual(controller.verify_controller_source_copy(root, sources=sources,
                                                                      identity=plan["identity"]), ci_config_with(sources))
            self.assertEqual(verify.call_args.args, (root,))
            (Path(directory) / ".git").write_bytes(b"inert forbidden metadata")
            with self.assertRaisesRegex(MbError, "omit Git metadata"):
                controller.verify_controller_source_copy(root, sources=sources, identity=plan["identity"])

    def test_forged_receipts_cannot_bypass_hash_mode_type_or_identity_checks(self):
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        from dataclasses import replace
        for receipt in (None, replace(sources, controller_sha="e" * 40), replace(sources, files=()),
                        replace(sources, files=(replace(sources.files[0], data=b"changed"), *sources.files[1:])),
                        replace(sources, files=(replace(sources.files[0], mode=[]), *sources.files[1:]))):
            with self.subTest(receipt=receipt), self.assertRaises(MbError):
                controller.verify_controller_source_copy(Path("unused"), sources=receipt, identity=plan["identity"])

    def test_invalid_materialization_receipt_rejects_before_output_allocation(self):
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        from dataclasses import replace
        for receipt in (None, replace(sources, controller_sha="e" * 40),
                        replace(sources, files=(replace(sources.files[0], data=b"changed"), *sources.files[1:]))):
            with self.subTest(receipt=receipt), patch.object(controller, "atomic_directory") as publish, self.assertRaises(MbError):
                controller.materialize_controller_sources(Path("unused"), sources=receipt, identity=plan["identity"])
            publish.assert_not_called()

    def test_materialization_requires_independent_stage_verification_before_publication(self):
        plan, api, _, protected = self.fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        stages = []
        def publication(output, writer):
            stages.append(output)
            return writer(Path("private-stage"), 17)
        with patch.object(controller, "atomic_directory", side_effect=publication), \
                patch.object(controller, "_write_controller_files") as writing, \
                patch.object(controller, "verify_controller_source_copy", side_effect=MbError("changed stage")) as verify, \
                self.assertRaises(MbError):
            controller.materialize_controller_sources(Path("sealed"), sources=sources, identity=plan["identity"])
        writing.assert_called_once_with(17, (sources.config, *sources.files))
        verify.assert_called_once_with(Path("private-stage"), sources=sources, identity=plan["identity"])
        self.assertEqual(stages, [Path("sealed")])


class ControllerReadHandoffTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/candidate-home")
    validator = WorkerAccount("validator", 2001, 2001, "/tmp/validator-home")

    def exercise(self, *, foreign=False, final_inode=30, grant_error=None, changed=False, alone=False,
                 subject=False):
        plan, api, _, protected = ControllerSourceTests().fixture(empty_module=True)
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        expected = ci_config_with(sources)
        def metadata(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group,
                                   st_mode=stat.S_IFDIR | mode)
        info = [metadata(20, 1001, 121, 0o711), metadata(21, 1001, 121, 0o711),
                metadata(30, 2000 if foreign else 1001, 121, 0o700),
                metadata(final_inode, 1001, 2001, 0o750)]
        events = []
        def record(name, value):
            def called(*args, **kwargs):
                events.append(name)
                if name == "grant" and grant_error:
                    raise grant_error
                return value
            return called
        with ExitStack() as stack:
            host = stack.enter_context(patch.object(controller, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(controller, "authenticate_worker_account", return_value=self.validator))
            peer = stack.enter_context(patch.object(controller, "authenticate_peer_account",
                                                    return_value=None if alone else self.candidate))
            stack.enter_context(patch.object(controller, "_open_directory", side_effect=[10, 11, 12]))
            stack.enter_context(patch.object(controller.os, "fstat", side_effect=info))
            stack.enter_context(patch.object(controller.os, "close"))
            modes = stack.enter_context(patch.object(controller.os, "fchmod", create=True))
            stack.enter_context(patch.object(controller.os, "fsync"))
            kill = stack.enter_context(patch.object(controller, "terminate_worker", side_effect=record("terminate", None)))
            stack.enter_context(patch.object(controller, "verify_controller_source_copy", side_effect=record("verify", expected)))
            grant = stack.enter_context(patch.object(controller, "grant_source_read_access", side_effect=record("grant", [])))
            check = stack.enter_context(patch.object(controller, "_verify_controller_copy",
                                                     side_effect=record("recheck", {} if changed else expected)))
            identity = subject_of(plan["identity"]) if subject else plan["identity"]
            try:
                self.assertEqual(controller.prepare_controller_validation(boundary=self.boundary,
                                 validator=self.validator, sources=sources, identity=identity), expected)
                peer.assert_called_once_with(self.validator, runner_uid=1001, runner_gid=121)
                if alone:
                    self.assertEqual(events, ["verify", "grant", "recheck"])
                    kill.assert_not_called()
                else:
                    self.assertEqual(events, ["terminate", "verify", "grant", "recheck"])
                    kill.assert_called_once_with(self.candidate)
                self.assertEqual(grant.call_args.args, (controller.CONTROLLER_VALIDATION_ROOT,))
                self.assertEqual(grant.call_args.kwargs["reader_gid"], self.validator.gid)
                self.assertEqual(len(grant.call_args.kwargs["tracked_paths"]), 5)
                self.assertTrue(check.call_args.kwargs["read_only"])
                self.assertEqual(host.call_count, 2)
                modes.assert_not_called()
            except MbError:
                if foreign:
                    modes.assert_not_called()
                else:
                    modes.assert_called_once_with(12, 0o700)
                raise

    def test_fixed_source_layout_account_and_access_order(self):
        self.exercise()

    def test_a_job_with_the_validator_alone_has_no_candidate_to_stop(self):
        self.exercise(alone=True)

    def test_the_subject_of_a_job_without_a_plan_binds_the_same_copy(self):
        self.exercise(subject=True)

    def test_accounts_that_are_not_isolated_reject_before_directory_open(self):
        plan, api, _, protected = ControllerSourceTests().fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        with patch.object(controller, "authenticate_privileged_host_boundary"), \
                patch.object(controller, "authenticate_worker_account", return_value=self.validator), \
                patch.object(controller, "authenticate_peer_account", side_effect=WorkerError("not isolated")), \
                patch.object(controller, "_open_directory") as opening, self.assertRaises(MbError):
            controller.prepare_controller_validation(boundary=self.boundary, validator=self.validator,
                                                      sources=sources, identity=plan["identity"])
        opening.assert_not_called()

    def test_foreign_copy_grant_identity_and_final_config_failures_stay_private(self):
        for changed in ({"foreign": True}, {"grant_error": OSError("failed transfer")},
                        {"final_inode": 31}, {"changed": True}):
            with self.subTest(changed=changed), self.assertRaises(MbError):
                self.exercise(**changed)

    def test_forged_validator_identity_rejects_before_directory_open(self):
        plan, api, _, protected = ControllerSourceTests().fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        with patch.object(controller, "authenticate_privileged_host_boundary"), \
                patch.object(controller, "authenticate_worker_account", return_value=self.candidate), \
                patch.object(controller, "_open_directory") as opening, self.assertRaises(MbError):
            controller.prepare_controller_validation(boundary=self.boundary, validator=self.validator,
                                                      sources=sources, identity=plan["identity"])
        opening.assert_not_called()


class ControllerExecutionTests(unittest.TestCase):
    boundary = ControllerReadHandoffTests.boundary
    validator = ControllerReadHandoffTests.validator
    candidate = ControllerReadHandoffTests.candidate

    def exercise(self, *, hook="verify_build", unit_id=None, pre_error=None, post_change=False,
                 execution_error=None, cleanup_error=None, subject=None, without_plan=False, peer_error=None):
        plan, api, _, protected = ControllerSourceTests().fixture()
        sources = controller.authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        config = ci_config_with(sources)
        tools = ToolTreeProof(("/opt/hostedtoolcache/python",), "a" * 64, 1, 2, 100)
        expected = WorkerResult(0, b"inert verifier result", False)
        reads = [config, {} if post_change else config]
        identity = plan["identity"] if subject is None else subject
        with patch.object(controller, "authenticate_worker_account", return_value=self.validator), \
                patch.object(controller, "authenticate_peer_account", side_effect=peer_error,
                             return_value=self.candidate) as peer, \
                patch.object(controller, "authenticate_host_boundary"), \
                patch.object(controller, "_authenticate_controller_read_copy", side_effect=pre_error or reads) as checking, \
                patch.object(controller, "execute_tool_fenced_worker", side_effect=execution_error, return_value=expected) as execute, \
                patch.object(controller, "terminate_worker", side_effect=cleanup_error) as terminate:
            try:
                result = controller.execute_controller_validator(boundary=self.boundary, validator=self.validator,
                         sources=sources, tools=tools, plan=None if without_plan else plan, hook=hook,
                         unit_id=unit_id, python="/opt/hostedtoolcache/python/bin/python", java_home=None,
                         run_id=42, run_attempt=2, **({} if subject is None else {"subject": subject}))
                self.assertEqual(result, expected)
                peer.assert_called_once_with(self.validator, runner_uid=1001, runner_gid=121)
                self.assertEqual(execute.call_args.kwargs["identity"], identity)
                self.assertEqual([call.args[3] for call in checking.call_args_list], [identity, identity])
                argv = execute.call_args.kwargs["command"]
                self.assertEqual(argv, ("/opt/hostedtoolcache/python/bin/python", "-I", "-B",
                                       str(controller.CONTROLLER_VALIDATION_ROOT / config["adapter"]["dispatcher"]),
                                       "--hook", hook))
                self.assertEqual(execute.call_args.kwargs["timeout_seconds"], config["timeouts"]["validator_seconds"])
                values = ({} if unit_id is None else
                          {"MB_TARGET_ID" if hook == "verify_target" else "MB_LANE_ID": unit_id})
                self.assertEqual(execute.call_args.kwargs["values"], values)
                self.assertEqual(checking.call_count, 2)
            finally:
                terminate.assert_called_once_with(self.validator)
                if (pre_error or peer_error or hook not in controller.VALIDATOR_HOOKS
                        or (hook == "verify_build" and unit_id is not None)):
                    execute.assert_not_called()

    def test_closed_hooks_bind_dispatcher_units_timeout_and_sanitized_values(self):
        self.assertEqual(controller.VALIDATOR_HOOKS, frozenset(adapter.PROTECTED_HOOKS))
        for hook, unit in (("verify_build", None), ("verify_target", "target-a"), ("verify_runtime", "lane-a"),
                           ("derive_runtime", "lane-a")):
            with self.subTest(hook=hook):
                self.exercise(hook=hook, unit_id=unit)

    def test_plan_derivation_runs_for_the_subject_and_never_for_a_plan(self):
        plan = ControllerSourceTests().fixture()[0]
        subject = subject_of(plan["identity"])
        self.exercise(hook="derive_plan", subject=subject, without_plan=True)
        rejected = [dict(hook="derive_plan"),                                    # a plan and no subject
                    dict(hook="derive_plan", without_plan=True),                 # neither
                    dict(hook="derive_plan", subject=subject),                   # both
                    dict(hook="derive_plan", subject=subject, without_plan=True, unit_id="target-a"),
                    dict(hook="derive_plan", subject=plan["identity"], without_plan=True, unit_id="lane-a"),
                    dict(hook="derive_plan", subject={**subject, "tested_sha": "x"}, without_plan=True),
                    dict(hook="verify_build", subject=subject),                  # the plan is the identity
                    dict(hook="verify_build", without_plan=True),
                    dict(hook="verify_build", subject=subject, without_plan=True),
                    dict(hook="derive_runtime", subject=subject, without_plan=True, unit_id="lane-a")]
        for arguments in rejected:
            with self.subTest(arguments=list(arguments)), self.assertRaises(MbError):
                self.exercise(**arguments)

    def test_planning_unknown_or_aggregate_unit_requests_reject_and_lock_without_launch(self):
        for hook, unit in (("derive_plan", None), ("sh", None), ("verify_build", "target-a"),
                           ("verify_target", "foreign"), ("verify_runtime", None), ("derive_runtime", None),
                           ("derive_runtime", "target-a"), ("policy", None), ("build_target", "target-a"),
                           ("run_lane", "lane-a"), (None, None)):
            with self.subTest(hook=hook), self.assertRaises(MbError):
                self.exercise(hook=hook, unit_id=unit)

    def test_accounts_that_are_not_isolated_never_launch(self):
        with self.assertRaisesRegex(MbError, "not isolated"):
            self.exercise(peer_error=WorkerError("worker identities are not isolated"))

    def test_admission_execution_postcheck_and_final_kill_failures_never_return_success(self):
        for args in ({"pre_error": WorkerError("unsafe sources")}, {"post_change": True},
                     {"execution_error": WorkerError("native failure")}, {"cleanup_error": WorkerError("survivor")},
                     {"pre_error": OSError("unreadable sources")}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)


def ci_config_with(sources):
    from mod_base.io.secure_json import loads
    return loads(sources.config.data, label=controller.BUILD_CONFIG_PATH,
                 max_bytes=limits.MAX_CI_CONFIG_BYTES)
