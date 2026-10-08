"""Strict data/context and explicit Windows I/O seams; real UID proof belongs to Linux."""

import base64
import copy
import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import handoff
from mod_base.build_ci.controller import authenticate_controller_sources
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.inputs import BuildValidationExecution
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_envelope, ci_execution


class ExecutionHandoffTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    validator = WorkerAccount("validator", 2001, 2001, "validator-home")

    def fixture(self):
        from tests.test_ci_controller import ControllerSourceTests
        plan, api, _, protected = ControllerSourceTests().fixture()
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        envelope = ci_envelope()
        bound = BuildValidationExecution(WorkerResult(0, b"binary\0\xff\n::candidate-log::", True),
                                         hashlib.sha256(canonical_json(envelope)).hexdigest())
        return dict(boundary=self.boundary, sources=sources, plan=plan, envelope=envelope,
                    bound=bound, run_id=42, run_attempt=2)

    def record(self, fixture):
        bound = fixture["bound"]
        return {"kind": "mod-base.ci.execution", "schema_version": 1,
                **handoff._context(fixture["sources"], fixture["plan"], fixture["envelope"], 42, 2),
                "nonce": "a" * 64, "returncode": 0, "truncated": bound.execution.truncated,
                "log_base64": base64.b64encode(bound.execution.log).decode("ascii")}

    def test_new_closed_kind_and_canonical_binary_log(self):
        document = ci_execution()
        self.assertEqual(load_document(canonical_json(document), kind=document["kind"]), document)
        for mutate in (lambda d: d.update(command="unsafe"), lambda d: d.update(returncode=True),
                       lambda d: d.update(returncode=1), lambda d: d.update(truncated=1),
                       lambda d: d.update(schema_version=2), lambda d: d.update(nonce="bad"),
                       lambda d: d.update(run_attempt=True), lambda d: d.pop("source_config_sha256"),
                       lambda d: d.update(log_base64="Zh=="), lambda d: d.update(log_base64=" Zg=="),
                       lambda d: d.update(log_base64="invalid"), lambda d: d.update(log_base64="\u00e9")):
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                handoff.validate_execution_handoff(changed)
        document["log_base64"] = ""
        handoff.validate_execution_handoff(document)
        document["log_base64"] = base64.b64encode(b"123").decode("ascii")
        with patch.object(limits, "MAX_CI_LOG_BYTES", 2), self.assertRaises(MbError):
            handoff.validate_execution_handoff(document)

    def test_writer_publishes_only_fixed_private_bytes_and_fresh_nonce(self):
        fixture = self.fixture()
        captured = {}
        def publish(output, writer):
            self.assertEqual(output, Path(str(handoff.EXECUTION_HANDOFF_ROOT)))
            writer(Path("private-stage"), 100)
        def write(fd, name, raw):
            self.assertEqual((fd, name), (100, grammar.CI_EXECUTION_NAME))
            captured["raw"] = raw
        def info(fd):
            return SimpleNamespace(st_mode=(stat.S_IFDIR | 0o700) if fd == 100 else (stat.S_IFREG | 0o600),
                                   st_uid=self.boundary.uid, st_gid=self.boundary.gid,
                                   st_nlink=1, st_size=len(captured.get("raw", b"x")))
        with ExitStack() as stack:
            auth = stack.enter_context(patch.object(handoff, "authenticate_host_boundary"))
            stack.enter_context(patch.object(handoff, "_layout"))
            stack.enter_context(patch.object(handoff, "atomic_directory", side_effect=publish))
            stack.enter_context(patch.object(handoff, "write_new", side_effect=write))
            stack.enter_context(patch.object(handoff.os, "urandom", return_value=b"\x0a" * 32))
            stack.enter_context(patch.object(handoff.os, "fstat", side_effect=info))
            stack.enter_context(patch.object(handoff.os, "open", return_value=200))
            stack.enter_context(patch.object(handoff.os, "O_NOFOLLOW", 0, create=True))
            chmod = stack.enter_context(patch.object(handoff.os, "fchmod", create=True))
            stack.enter_context(patch.object(handoff.os, "fsync"))
            stack.enter_context(patch.object(handoff.os, "close"))
            stack.enter_context(patch.object(handoff, "read_child_file", side_effect=lambda *a, **k: captured["raw"]))
            nonce = handoff.record_build_validation_execution(**fixture)
        expected = self.record(fixture)
        expected["nonce"] = "0a" * 32
        self.assertEqual(nonce, expected["nonce"])
        self.assertEqual(captured["raw"], canonical_json(expected))
        chmod.assert_called_once_with(200, 0o600)
        self.assertEqual(auth.call_count, 2)

    def test_wrong_or_failed_execution_and_context_reject_before_publication(self):
        fixture = self.fixture()
        original = fixture["bound"]
        changes = [{"bound": None}, {"run_id": True}, {"run_id": 43}, {"run_attempt": 3},
                   {"bound": BuildValidationExecution(original.execution, "f" * 64)},
                   {"bound": BuildValidationExecution(WorkerResult(1, b"failed", False), original.input_sha256)},
                   {"bound": BuildValidationExecution(WorkerResult(False, b"", False), original.input_sha256)},
                   {"bound": BuildValidationExecution(WorkerResult(0, "text", False), original.input_sha256)}]
        for change in changes:
            with patch.object(handoff, "authenticate_host_boundary"), \
                    patch.object(handoff, "atomic_directory") as publish, self.subTest(change=change):
                with self.assertRaises(MbError):
                    handoff.record_build_validation_execution(**{**fixture, **change})
                publish.assert_not_called()

    def freeze(self, fixture, raw, *, nonce="a" * 64, forbid=False):
        with ExitStack() as stack:
            stack.enter_context(patch.object(handoff, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(handoff, "_accounts"))
            stack.enter_context(patch.object(handoff, "_layout"))
            stack.enter_context(patch.object(handoff, "_read_private_handoff", return_value=raw))
            freeze = stack.enter_context(patch.object(handoff, "freeze_frozen_build_validation",
                                                      return_value={"delegated": "receipt"}))
            terminate = stack.enter_context(patch.object(handoff, "terminate_worker"))
            args = {key: value for key, value in fixture.items() if key != "bound"}
            try:
                result = handoff.freeze_handed_off_build_validation(**args, validator=self.validator, nonce=nonce)
            finally:
                terminate.assert_called_once_with(self.validator)
                if forbid:
                    freeze.assert_not_called()
            return result, freeze.call_args.kwargs

    def test_root_delegates_retained_binary_result_without_a_program_or_hook_field(self):
        fixture = self.fixture()
        result, args = self.freeze(fixture, canonical_json(self.record(fixture)))
        self.assertEqual(result, {"delegated": "receipt"})
        self.assertEqual(args["bound"], fixture["bound"])
        self.assertEqual(args["sources"], fixture["sources"])
        self.assertNotIn("hook", args)

    def test_wrong_nonce_attempt_plan_source_input_and_noncanonical_bytes_never_freeze(self):
        fixture = self.fixture()
        original = self.record(fixture)
        for key, value in (("nonce", "b" * 64), ("run_id", 43), ("run_attempt", 3),
                           ("source_config_sha256", "b" * 64), ("plan_sha256", "b" * 64),
                           ("input_sha256", "b" * 64)):
            document = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(MbError):
                self.freeze(fixture, canonical_json(document), forbid=True)
        raw = canonical_json(original)
        for hostile in (b" " + raw, raw.replace(b'"returncode":0', b'"returncode":0,"returncode":0'),
                        b'{"returncode":NaN}', b"not JSON"):
            with self.subTest(raw=hostile[:30]), self.assertRaises(MbError):
                self.freeze(fixture, hostile, forbid=True)

    def test_role_rejection_precedes_accounts_io_cleanup_and_record_construction(self):
        fixture = self.fixture()
        with patch.object(handoff, "authenticate_host_boundary", side_effect=WorkerError("wrong role")), \
                patch.object(handoff, "_context") as context:
            with self.assertRaises(MbError):
                handoff.record_build_validation_execution(**fixture)
            context.assert_not_called()
        args = {key: value for key, value in fixture.items() if key != "bound"}
        with patch.object(handoff, "authenticate_privileged_host_boundary", side_effect=WorkerError("not root")), \
                patch.object(handoff, "_accounts") as accounts, patch.object(handoff, "terminate_worker") as stop:
            with self.assertRaises(MbError):
                handoff.freeze_handed_off_build_validation(**args, validator=self.validator, nonce="a" * 64)
            accounts.assert_not_called()
            stop.assert_not_called()

    def test_private_channel_rejects_foreign_permissions_links_special_empty_and_large_files(self):
        initial = dict(st_mode=stat.S_IFREG | 0o600, st_uid=self.boundary.uid, st_gid=self.boundary.gid,
                       st_nlink=1, st_size=100)
        handoff._private(SimpleNamespace(**initial), self.boundary, directory=False)
        for change in ({"st_uid": 2001}, {"st_gid": 2001}, {"st_mode": stat.S_IFREG | 0o644},
                       {"st_mode": stat.S_IFLNK | 0o600}, {"st_mode": stat.S_IFIFO | 0o600},
                       {"st_nlink": 2}, {"st_size": 0}, {"st_size": limits.MAX_CI_EXECUTION_BYTES + 1}):
            with self.subTest(change=change), self.assertRaises(MbError):
                handoff._private(SimpleNamespace(**{**initial, **change}), self.boundary, directory=False)
        for mode in (stat.S_IFDIR | 0o755, stat.S_IFLNK | 0o700, stat.S_IFREG | 0o700):
            with self.subTest(mode=mode), self.assertRaises(MbError):
                handoff._private(SimpleNamespace(**{**initial, "st_mode": mode}), self.boundary, directory=True)

    def test_channel_io_errors_stop_admitted_validator_and_never_authorize_receipt(self):
        fixture = self.fixture()
        args = {key: value for key, value in fixture.items() if key != "bound"}
        with patch.object(handoff, "authenticate_privileged_host_boundary"), \
                patch.object(handoff, "_accounts"), patch.object(handoff, "_layout"), \
                patch.object(handoff, "_read_private_handoff", side_effect=OSError("missing channel")), \
                patch.object(handoff, "freeze_frozen_build_validation") as freeze, \
                patch.object(handoff, "terminate_worker") as stop:
            with self.assertRaises(WorkerError):
                handoff.freeze_handed_off_build_validation(**args, validator=self.validator, nonce="a" * 64)
            freeze.assert_not_called()
            stop.assert_called_once_with(self.validator)
        for failing in ("authenticate_host_boundary", "_layout", "atomic_directory"):
            with ExitStack() as stack:
                for name in ("authenticate_host_boundary", "_layout", "atomic_directory"):
                    stack.enter_context(patch.object(handoff, name, side_effect=OSError("I/O failure")
                                                     if name == failing else None))
                with self.subTest(operation=failing), self.assertRaises(WorkerError):
                    handoff.record_build_validation_execution(**fixture)


if __name__ == "__main__":
    unittest.main()
