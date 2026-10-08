"""Closed metadata-only root-freeze requests; no physical source/execution claims."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.controller import BUILD_CONFIG_PATH
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_root_request


class RootRequestSchemaTests(unittest.TestCase):
    def test_new_closed_kind_rejects_program_path_permissions_and_inexact_types(self):
        document = ci_root_request()
        self.assertEqual(document["sources"]["config"]["path"], BUILD_CONFIG_PATH)
        self.assertEqual(load_document(canonical_json(document), kind=document["kind"]), document)
        for mutate in (lambda d: d.update(program="unsafe"), lambda d: d.update(command="unsafe"),
                       lambda d: d.update(path="/tmp/candidate"), lambda d: d.update(permissions={}),
                       lambda d: d.update(schema_version=2), lambda d: d.update(run_attempt=True),
                       lambda d: d["boundary"].update(device=True),
                       lambda d: d["boundary"].update(home="/tmp/runner"),
                       lambda d: d["boundary"].update(inode=limits.MAX_CI_FILE_ID+1),
                       lambda d: d["validator"].update(uid=0),
                       lambda d: d["validator"].update(uid=limits.MIN_CI_WORKER_UID-1),
                       lambda d: d["validator"].update(gid=limits.MIN_CI_WORKER_UID-1),
                       lambda d: d["sources"]["config"].update(size=True),
                       lambda d: d["sources"]["files"][0].update(mode="120000"),
                       lambda d: d["sources"]["files"][0].update(data_base64="")):
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(MbError):
                validate_root_request(changed)

    def test_context_and_distinct_nonce_bindings_reject_mixed_requests(self):
        for mutate in (lambda d: d.update(nonce=d["execution_nonce"]), lambda d: d.update(run_id=43),
                       lambda d: d["sources"].update(controller_sha="e"*40),
                       lambda d: d["validator"].update(uid=d["boundary"]["uid"]),
                       lambda d: d["validator"].update(gid=d["boundary"]["gid"]),
                       lambda d: d["sources"]["config"].update(path="scripts/ci/other.json"),
                       lambda d: d["sources"]["files"][0].update(path=".git/config"),
                       lambda d: d["sources"]["files"].append(copy.deepcopy(d["sources"]["files"][0])),
                       lambda d: d["envelope"].update(plan_sha256="f"*64)):
            changed = ci_root_request()
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                validate_root_request(changed)

    def test_existing_native_and_document_caps_are_preserved(self):
        document = ci_root_request()
        for bound in ("MAX_CI_PLAN_BYTES", "MAX_CI_ENVELOPE_BYTES", "MAX_CI_ROOT_REQUEST_BYTES",
                      "MAX_CI_ADAPTER_TREE_BYTES", "MAX_CI_CONFIG_BYTES"):
            with self.subTest(bound=bound), patch.object(limits,bound,1), self.assertRaises(MbError):
                validate_root_request(document)
        for target, size in (("config",limits.MAX_CI_CONFIG_BYTES+1),
                              ("file",limits.MAX_CI_ADAPTER_FILE_BYTES+1)):
            changed = ci_root_request()
            record = changed["sources"]["config"] if target=="config" else changed["sources"]["files"][0]
            record["size"] = size
            with self.subTest(target=target), self.assertRaises(MbError):
                validate_root_request(changed)

    def test_strict_json_ambiguity_rejected_before_context_admission(self):
        raw = canonical_json(ci_root_request())
        for changed in (raw.replace(b'"schema_version":1',b'"schema_version":1,"schema_version":1',1),
                        raw.replace(b'"run_attempt":2',b'"run_attempt":NaN',1),b"\xff"):
            with self.subTest(raw=changed[:30]), self.assertRaises(MbError):
                load_document(changed,kind="mod-base.ci.root-request")
