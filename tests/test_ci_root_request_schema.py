"""Closed root-operation requests: data only, no program, path or authority field."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci import root_request_schema
from mod_base.build_ci.controller import BUILD_CONFIG_PATH
from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_root_request


BUILD, RUNTIME = "freeze-build-validation", "freeze-runtime-validation"
#: The operations of ``ci worker-prepare``, ``ci plan`` and the read grants of ``ci worker-validate``:
#: each names the accounts of the job.
LIFECYCLE = ("grant-controller", "grant-plan-inputs", "take-derived-plan", "grant-validation-inputs",
             "grant-build-validation", "grant-runtime-validation")


class RootRequestSchemaTests(unittest.TestCase):
    def test_every_closed_operation_has_exactly_one_argument_contract(self):
        self.assertEqual(tuple(root_request_schema._OPERATIONS), grammar.CI_ROOT_OPERATIONS)
        self.assertEqual(len(set(grammar.CI_ROOT_OPERATIONS)), len(grammar.CI_ROOT_OPERATIONS))
        for operation in grammar.CI_ROOT_OPERATIONS:
            with self.subTest(operation=operation):
                document = ci_root_request(operation)
                self.assertEqual(load_document(canonical_json(document), kind=document["kind"]), document)

    def test_one_operation_never_accepts_the_arguments_of_another(self):
        for operation in grammar.CI_ROOT_OPERATIONS:
            for other in grammar.CI_ROOT_OPERATIONS:
                if other == operation:
                    continue
                document = ci_root_request(operation)
                document["operation"] = other
                with self.subTest(operation=operation, other=other), self.assertRaises(MbError):
                    validate_root_request(document)
        for operation in ("", "freeze", "FREEZE-BUILD-VALIDATION", "freeze-build-validation ", None, 7):
            document = ci_root_request()
            document["operation"] = operation
            with self.subTest(operation=operation), self.assertRaises(MbError):
                validate_root_request(document)

    def test_closed_kind_rejects_program_path_permissions_and_inexact_types(self):
        for operation in (BUILD, RUNTIME):
            document = ci_root_request(operation)
            self.assertEqual(document["arguments"]["sources"]["config"]["path"], BUILD_CONFIG_PATH)
            for mutate in (lambda d: d.update(program="unsafe"), lambda d: d.update(command="unsafe"),
                           lambda d: d.update(path="/tmp/candidate"), lambda d: d.update(permissions={}),
                           lambda d: d.update(schema_version=2), lambda d: d.update(arguments=[]),
                           lambda d: d["arguments"].update(program="unsafe"),
                           lambda d: d["arguments"].update(hook="verify_build"),
                           lambda d: d["arguments"].update(path="/tmp/export"),
                           lambda d: d["arguments"].update(run_attempt=True),
                           lambda d: d["arguments"].update(run_id=True),
                           lambda d: d["boundary"].update(device=True),
                           lambda d: d["boundary"].update(home="/tmp/runner"),
                           lambda d: d["boundary"].update(inode=limits.MAX_CI_FILE_ID + 1),
                           lambda d: d["arguments"]["validator"].update(uid=0),
                           lambda d: d["arguments"]["validator"].update(uid=limits.MIN_CI_WORKER_UID - 1),
                           lambda d: d["arguments"]["validator"].update(gid=limits.MIN_CI_WORKER_UID - 1),
                           lambda d: d["arguments"]["sources"]["config"].update(size=True),
                           lambda d: d["arguments"]["sources"]["files"][0].update(mode="120000"),
                           lambda d: d["arguments"]["sources"]["files"][0].update(data_base64="")):
                changed = copy.deepcopy(document)
                mutate(changed)
                with self.subTest(operation=operation, changed=str(changed)[:80]), self.assertRaises(MbError):
                    validate_root_request(changed)

    def test_context_account_and_distinct_nonce_bindings_reject_mixed_requests(self):
        shared = (lambda d: d.update(nonce=d["arguments"]["execution_nonce"]),
                  lambda d: d["arguments"]["sources"].update(controller_sha="e" * 40),
                  lambda d: d["arguments"]["validator"].update(uid=d["boundary"]["uid"]),
                  lambda d: d["arguments"]["validator"].update(gid=d["boundary"]["gid"]),
                  lambda d: d["arguments"]["sources"]["config"].update(path="scripts/ci/other.json"),
                  lambda d: d["arguments"]["sources"]["files"][0].update(path=".git/config"),
                  lambda d: d["arguments"]["sources"]["files"].append(
                      copy.deepcopy(d["arguments"]["sources"]["files"][0])))
        cases = {
            BUILD: (*shared, lambda d: d["arguments"].update(run_id=43),
                    lambda d: d["arguments"]["envelope"].update(plan_sha256="f" * 64)),
            RUNTIME: (*shared, lambda d: d["arguments"].update(lane_id="lane-b"),
                      lambda d: d["arguments"].update(run_id=42), lambda d: d["arguments"].update(run_attempt=3),
                      lambda d: d["arguments"]["build"].update(scope="target", target_id="target-a"),
                      lambda d: d["arguments"]["runtime"].update(scope="complete", lane_id=None),
                      lambda d: d["arguments"]["runtime"]["owning_build"]["producer"].update(run_id=44),
                      lambda d: d["arguments"]["runtime"].update(plan_sha256="f" * 64),
                      lambda d: d["arguments"]["build"].update(plan_sha256="f" * 64)),
        }
        for operation, mutations in cases.items():
            for index, mutate in enumerate(mutations):
                changed = ci_root_request(operation)
                mutate(changed)
                with self.subTest(operation=operation, index=index), self.assertRaises(MbError):
                    validate_root_request(changed)

    def test_lifecycle_operations_name_the_accounts_of_the_job_and_nothing_that_selects_code(self):
        for operation in LIFECYCLE:
            document = ci_root_request(operation)
            validate_root_request(document)
            alone = copy.deepcopy(document)
            alone["arguments"]["candidate"] = None  # A job that allocated the validator alone.
            validate_root_request(alone)
            for mutate in (lambda d: d["arguments"].pop("candidate"),
                           lambda d: d["arguments"].pop("validator"),
                           lambda d: d["arguments"].update(validator=None),
                           lambda d: d["arguments"]["candidate"].update(uid=d["arguments"]["validator"]["uid"]),
                           lambda d: d["arguments"]["candidate"].update(gid=d["arguments"]["validator"]["gid"]),
                           lambda d: d["arguments"]["candidate"].update(uid=d["boundary"]["uid"]),
                           lambda d: d["arguments"]["candidate"].update(gid=d["boundary"]["gid"]),
                           lambda d: d["arguments"]["validator"].update(uid=d["boundary"]["uid"]),
                           lambda d: d["arguments"]["candidate"].update(uid=limits.MIN_CI_WORKER_UID - 1),
                           lambda d: d["arguments"]["candidate"].update(home="/tmp/other"),
                           lambda d: d["arguments"].update(hook="derive_plan"),
                           lambda d: d["arguments"].update(path="/tmp/export"),
                           lambda d: d["arguments"].update(command=["sh"])):
                changed = copy.deepcopy(document)
                mutate(changed)
                with self.subTest(operation=operation, changed=str(changed["arguments"])[:80]), \
                        self.assertRaises(MbError):
                    validate_root_request(changed)

    def test_lifecycle_operations_bind_their_sources_digests_and_plan(self):
        cases = {
            "grant-controller": (
                lambda d: d["arguments"]["sources"].update(controller_sha="e" * 40),
                lambda d: d["arguments"]["subject"].update(controller_sha="e" * 40),
                lambda d: d["arguments"]["subject"].update(policy_sha256="f" * 64),  # A subject, not an identity.
                lambda d: d["arguments"]["subject"].pop("tested_tree"),
                lambda d: d["arguments"]["sources"]["config"].update(path="scripts/ci/other.json"),
                lambda d: d["arguments"]["sources"]["files"][0].update(path=".git/config"),
                lambda d: d["arguments"]["sources"]["files"][0].update(data_base64=""),
                lambda d: d["arguments"].update(plan=d["arguments"]["subject"])),
            "grant-plan-inputs": (
                lambda d: d["arguments"]["inputs"][0].update(sha256="A" * 64),
                lambda d: d["arguments"]["inputs"][1].update(sha256="b" * 63),
                lambda d: d["arguments"]["inputs"].pop(0),  # No inventory.
                lambda d: d["arguments"]["inputs"].reverse(),
                lambda d: d["arguments"]["inputs"].append({"name": "another", "sha256": "c" * 64}),  # Not sorted.
                lambda d: d["arguments"]["inputs"].append(dict(d["arguments"]["inputs"][2])),
                lambda d: d["arguments"]["inputs"][2].update(name="inventory"),
                lambda d: d["arguments"]["inputs"][2].update(name="ci-plan.json"),
                lambda d: d["arguments"]["inputs"][2].update(name="Gradle Properties"),
                lambda d: d["arguments"]["inputs"][2].update(path="gradle.properties"),
                lambda d: d["arguments"]["inputs"].extend(
                    {"name": f"input-{index}", "sha256": "c" * 64} for index in range(limits.MAX_CI_PLAN_INPUTS)),
                lambda d: d["arguments"].pop("inputs"),
                lambda d: d["arguments"].update(inventory_sha256="a" * 64)),
            "take-derived-plan": (
                lambda d: d["arguments"].update(output="plan.json"),
                lambda d: d["arguments"].update(inventory_sha256="a" * 64)),
            "grant-validation-inputs": (
                lambda d: d["arguments"]["plan"].update(plan_sha256="f" * 64),
                lambda d: d["arguments"]["plan"]["identity"].update(inventory_sha256="f" * 64),
                lambda d: d["arguments"].update(plan=None),
                lambda d: d["arguments"].update(inventory_sha256="a" * 64)),
            "grant-build-validation": (
                lambda d: d["arguments"]["envelope"].update(plan_sha256="f" * 64),
                lambda d: d["arguments"]["plan"].update(plan_sha256="f" * 64),
                lambda d: d["arguments"]["envelope"]["files"].pop(),  # Not the planned output union.
                lambda d: d["arguments"]["envelope"].update(scope="target"),
                lambda d: d["arguments"].update(envelope=None),
                lambda d: d["arguments"].pop("envelope"),
                lambda d: d["arguments"].update(run_id=42),
                lambda d: d["arguments"].update(sources={}),
                lambda d: d["arguments"].update(execution_nonce="a" * 64)),
            "grant-runtime-validation": (
                lambda d: d["arguments"].update(lane_id="lane-b"),
                lambda d: d["arguments"].update(run_id=42), lambda d: d["arguments"].update(run_attempt=3),
                lambda d: d["arguments"]["build"].update(scope="target", target_id="target-a"),
                lambda d: d["arguments"]["runtime"].update(scope="complete", lane_id=None),
                lambda d: d["arguments"]["runtime"]["owning_build"]["producer"].update(run_id=44),
                lambda d: d["arguments"]["runtime"].update(plan_sha256="f" * 64),
                lambda d: d["arguments"]["build"].update(plan_sha256="f" * 64),
                lambda d: d["arguments"].pop("build"),
                lambda d: d["arguments"].update(sources={}),
                lambda d: d["arguments"].update(execution_nonce="a" * 64)),
        }
        self.assertEqual(tuple(cases), LIFECYCLE)
        for operation, mutations in cases.items():
            for index, mutate in enumerate(mutations):
                changed = ci_root_request(operation)
                mutate(changed)
                with self.subTest(operation=operation, index=index), self.assertRaises(MbError):
                    validate_root_request(changed)
        with patch.object(limits, "MAX_CI_PLAN_BYTES", 1), self.assertRaises(MbError):
            validate_root_request(ci_root_request("grant-validation-inputs"))

    def test_runtime_request_keeps_its_cross_run_owning_build(self):
        for operation in (RUNTIME, "grant-runtime-validation"):
            arguments = ci_root_request(operation)["arguments"]
            self.assertEqual(arguments["build"]["producer"]["run_id"], 42)
            self.assertEqual(arguments["runtime"]["producer"]["run_id"], 43)

    def test_the_build_grant_takes_a_partition_or_the_complete_build_of_any_run(self):
        from tests.helpers import ci_envelope

        document = ci_root_request("grant-build-validation")
        self.assertEqual(document["arguments"]["envelope"]["scope"], "complete")
        partition = copy.deepcopy(document)
        partition["arguments"]["envelope"] = ci_envelope(target_id="target-a")
        validate_root_request(partition)
        # The owning Build of a lane was sealed by another run: the request names no attempt.
        self.assertEqual(sorted(document["arguments"]), ["candidate", "envelope", "plan", "validator"])
        with patch.object(limits, "MAX_CI_ENVELOPE_BYTES", 1), self.assertRaises(MbError):
            validate_root_request(document)

    def test_existing_native_and_document_caps_are_preserved(self):
        for operation, bounds in ((BUILD, ("MAX_CI_PLAN_BYTES", "MAX_CI_ENVELOPE_BYTES", "MAX_CI_ROOT_REQUEST_BYTES",
                                           "MAX_CI_ADAPTER_TREE_BYTES", "MAX_CI_CONFIG_BYTES")),
                                  (RUNTIME, ("MAX_CI_PLAN_BYTES", "MAX_CI_ENVELOPE_BYTES",
                                             "MAX_CI_RUNTIME_ENVELOPE_BYTES", "MAX_CI_ROOT_REQUEST_BYTES",
                                             "MAX_CI_CONFIG_BYTES", "MAX_CI_ADAPTER_TREE_BYTES"))):
            document = ci_root_request(operation)
            for bound in bounds:
                with self.subTest(operation=operation, bound=bound), patch.object(limits, bound, 1), \
                        self.assertRaises(MbError):
                    validate_root_request(document)
        for target, size in (("config", limits.MAX_CI_CONFIG_BYTES + 1),
                             ("file", limits.MAX_CI_ADAPTER_FILE_BYTES + 1)):
            changed = ci_root_request()
            sources = changed["arguments"]["sources"]
            record = sources["config"] if target == "config" else sources["files"][0]
            record["size"] = size
            with self.subTest(target=target), self.assertRaises(MbError):
                validate_root_request(changed)

    def test_strict_json_ambiguity_rejected_before_context_admission(self):
        for operation in (BUILD, RUNTIME):
            raw = canonical_json(ci_root_request(operation))
            for changed in (raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1),
                            raw.replace(b'"run_attempt":2', b'"run_attempt":NaN', 1), b"\xff"):
                with self.subTest(operation=operation, raw=changed[:30]), self.assertRaises(MbError):
                    load_document(changed, kind="mod-base.ci.root-request")


if __name__ == "__main__":
    unittest.main()
