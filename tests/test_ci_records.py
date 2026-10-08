"""Canonical descriptors, complete envelopes/gates and non-chaining reference regressions."""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci.authenticate import authenticate_merged_pr_identity
from mod_base.build_ci.records import (bind_build_envelope, bind_gate_receipt, bind_reuse_reference,
                                       validate_build_envelope, validate_descriptor,
                                       validate_gate_receipt, validate_reuse_reference, validate_source_selection)
from mod_base.build_ci.protocol import plan_sha256, validate_plan
from mod_base.build_ci.config import validate_build_config
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document, validate_document
from tests.helpers import ci_config, ci_descriptor, ci_envelope, ci_gate, ci_plan, ci_reuse, ci_selection
from tests import test_ci_protocol as protocol_fixtures


def selected_record(document):
    kind = "tested" if document["kind"] == "mod-base.ci.gate" else "reuse"
    descriptor = ci_descriptor(kind, unit_id=document.get("gate"))
    for key in ("identity", "plan_sha256", "profile", "producer"):
        descriptor[key] = copy.deepcopy(document[key])
    descriptor["producer"]["upload_window"] = {
        "started_at": "2026-10-07T10:05:00Z", "completed_at": "2026-10-07T10:06:00Z"}
    descriptor["artifact"].update(id=200,
        name=grammar.ci_artifact_name(kind, document["producer"]["run_id"], document["producer"]["run_attempt"], document.get("gate")),
        created_at="2026-10-07T10:05:30Z")
    return descriptor


def same_commit_reuse():
    reference = ci_reuse()
    sha = reference["source"]["identity"]["tested_sha"]
    reference["identity"].update(tested_sha=sha, head_sha=sha, controller_sha=sha, base_sha=sha,
                                 tested_parents=copy.deepcopy(reference["source"]["identity"]["tested_parents"]))
    reference["producer"]["api_head_sha"] = sha
    return reference


class RecordUploadBindingTests(unittest.TestCase):
    def bind(self, document, descriptor):
        if document["kind"] == "mod-base.ci.gate":
            return bind_gate_receipt(document, descriptor=descriptor, plan=ci_plan())
        return bind_reuse_reference(document, descriptor=descriptor)

    def test_each_record_can_be_serialized_before_its_actual_upload_is_known(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            before = canonical_json(document)
            self.assertNotIn("upload_window", document["producer"])
            descriptor = selected_record(document)
            self.assertIs(self.bind(document, descriptor), document)
            self.assertEqual(canonical_json(document), before)

    def test_old_local_draft_window_shape_rejects_for_both_new_record_kinds(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            document["producer"]["upload_window"] = selected_record(document)["producer"]["upload_window"]
            with self.subTest(kind=document["kind"]), self.assertRaises(MbError):
                self.bind(document, selected_record(document))

    def test_actual_selected_record_window_is_still_mandatory(self):
        for document in (ci_gate(), ci_reuse()):
            descriptor = selected_record(document)
            descriptor["producer"].pop("upload_window")
            with self.subTest(kind=document["kind"]), self.assertRaises(MbError):
                self.bind(document, descriptor)

    def test_late_source_upload_cannot_satisfy_gate_or_reuse_binding(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            descriptor = selected_record(document)
            source = document["artifacts"][0] if document["kind"] == "mod-base.ci.gate" else document["source"]["build_seal"]
            source["producer"]["upload_window"] = {
                "started_at": "2026-10-07T10:04:00Z", "completed_at": "2026-10-07T10:06:00Z"}
            source["artifact"]["created_at"] = "2026-10-07T10:04:30Z"
            with self.subTest(kind=document["kind"]), self.assertRaisesRegex(MbError, "postdate"):
                self.bind(document, descriptor)

    def test_boundary_source_completion_equal_to_record_upload_start_is_allowed(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            descriptor = selected_record(document)
            descriptor["producer"]["upload_window"]["started_at"] = "2026-10-07T10:02:00Z"
            self.bind(document, descriptor)

    def test_record_id_cannot_collide_with_any_source(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            descriptor = selected_record(document)
            source = document["artifacts"][0] if document["kind"] == "mod-base.ci.gate" else document["source"]["build_seal"]
            descriptor["artifact"]["id"] = source["artifact"]["id"]
            with self.subTest(kind=document["kind"]), self.assertRaisesRegex(MbError, "collides"):
                self.bind(document, descriptor)

    def test_wrong_record_kind_gate_unit_writer_identity_and_plan_reject(self):
        for document in (ci_gate(), ci_gate("packaged"), ci_reuse()):
            for change in ("kind", "identity", "plan"):
                descriptor = selected_record(document)
                if change == "kind":
                    descriptor["artifact"]["name"] = grammar.ci_artifact_name("build",
                        descriptor["producer"]["run_id"], descriptor["producer"]["run_attempt"])
                elif change == "identity":
                    descriptor["producer"]["graph_sha256"] = "f" * 64
                else:
                    descriptor["plan_sha256"] = "f" * 64
                with self.subTest(kind=document["kind"], change=change), self.assertRaises(MbError):
                    self.bind(document, descriptor)

    def test_reuse_binding_accepts_an_independently_supplied_covered_plan(self):
        from mod_base.build_ci.protocol import plan_sha256
        document = ci_reuse()
        plan = ci_plan()
        plan["identity"] = copy.deepcopy(document["identity"])
        plan["plan_sha256"] = plan_sha256(plan)
        document["plan_sha256"] = plan["plan_sha256"]
        bind_reuse_reference(document, descriptor=selected_record(document), plan=plan)

    def test_owning_build_and_both_reuse_seals_must_precede_record_upload(self):
        cases = [(ci_gate("packaged"), "owning_build"), (ci_reuse(), "build_seal"), (ci_reuse(), "packaged_seal")]
        for document, key in cases:
            descriptor = selected_record(document)
            source = document[key] if key == "owning_build" else document["source"][key]
            source["producer"]["upload_window"] = {
                "started_at": "2026-10-07T10:06:00Z", "completed_at": "2026-10-07T10:07:00Z"}
            source["artifact"]["created_at"] = "2026-10-07T10:06:30Z"
            with self.subTest(source=key), self.assertRaisesRegex(MbError, "postdate"):
                self.bind(document, descriptor)

    def test_tested_seal_unit_cannot_switch_build_and_packaged_authority(self):
        for gate in ("build", "packaged"):
            document = ci_gate(gate)
            descriptor = selected_record(document)
            descriptor["artifact"]["name"] = grammar.ci_artifact_name("tested",
                descriptor["producer"]["run_id"], descriptor["producer"]["run_attempt"],
                "packaged" if gate == "build" else "build")
            with self.subTest(gate=gate), self.assertRaisesRegex(MbError, "wrong selected record kind"):
                self.bind(document, descriptor)


class DescriptorTests(unittest.TestCase):
    def test_closed_names_round_trip_and_pages_parser_excludes_every_ci_kind(self):
        for kind, unit in (("target", "target-a"), ("runtime", "lane-a"), ("build", None),
                           ("results", None), ("tested", "build"), ("tested", "packaged"), ("reuse", None)):
            with self.subTest(kind=kind, unit=unit):
                name = grammar.ci_artifact_name(kind, limits.MAX_RUN_ID, limits.MAX_RUN_ATTEMPT, unit)
                parsed = grammar.parse_ci_artifact_name(name)
                self.assertEqual((parsed.kind, parsed.run_id, parsed.run_attempt, parsed.unit_id),
                                 (kind, limits.MAX_RUN_ID, limits.MAX_RUN_ATTEMPT, unit))
                self.assertIsNone(grammar.parse_artifact_name(name), "Pages rotation must leave CI sources alone")

    def test_malformed_names_are_never_treated_as_other_ci_kinds(self):
        for name in ("mb-ci-build--042--a2", "mb-ci-build--42--a0", "mb-ci-build--42--a1001",
                     "mb-ci-runtime--42--a2", "mb-ci-build--42--a2--extra", "mb-ci-tested--42--a2--other",
                     "mb-ci-target--42--a2--a--b", "mb-ci-runtime--42--a2--../x", "\ud800"):
            self.assertIsNone(grammar.parse_ci_artifact_name(name))
        for arguments in (("build", True, 2), ("build", 42, True), ("target", 42, 2, "a--b"),
                          ("tested", 42, 2, "other"), ("results", 42, 2, "lane-a")):
            with self.assertRaises(MbError):
                grammar.ci_artifact_name(*arguments)

    def test_digest_and_exact_producer_attempt_are_mandatory(self):
        mutations = [lambda d: d["artifact"].pop("digest"),
                     lambda d: d["artifact"].update(digest="sha256:" + "x" * 64),
                     lambda d: d["artifact"].update(id=True), lambda d: d["producer"].update(run_attempt=1),
                     lambda d: d["producer"].update(event="pull_request"),
                     lambda d: d["producer"].update(workflow_ref="foreign/mod/.github/workflows/build-gate.yml@refs/heads/master"),
                     lambda d: d["artifact"].update(created_at="2026-10-07T10:00:00Z"),
                     lambda d: d["artifact"].update(expires_at="2026-10-07T10:01:30Z"),
                     lambda d: d["producer"]["upload_window"].update(completed_at="2026-10-07T10:00:00Z")]
        for index, mutate in enumerate(mutations):
            descriptor = ci_descriptor()
            mutate(descriptor)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_descriptor(descriptor)


class ConfigTests(unittest.TestCase):
    def test_fixed_entrypoints_are_inventoried_without_a_second_domain_catalog(self):
        config = ci_config()
        self.assertIs(validate_build_config(config), config)
        mutations = [lambda c: c.update(targets=["invented"]), lambda c: c.update(secrets="inherit"),
                     lambda c: c["adapter"].update(command="arbitrary program"),
                     lambda c: c["adapter"].update(dispatcher=c["adapter"]["path"]),
                     lambda c: c["adapter"]["files"].pop(),
                     lambda c: c["adapter"].update(path="candidate/main.py"),
                     lambda c: c["timeouts"].update(target_seconds=True),
                     lambda c: c["timeouts"].update(target_seconds=limits.MAX_CI_WORKER_TIMEOUT_SECONDS + 1)]
        for index, mutate in enumerate(mutations):
            config = ci_config()
            mutate(config)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_build_config(config)


class EnvelopeTests(unittest.TestCase):
    def test_export_is_serialized_before_actual_upload_window_is_known(self):
        envelope = ci_envelope()
        before = canonical_json(envelope)
        self.assertNotIn("upload_window", envelope["producer"])
        descriptor = ci_descriptor()
        descriptor["producer"]["upload_window"] = {
            "started_at": "2026-10-07T10:03:00Z", "completed_at": "2026-10-07T10:04:00Z"}
        descriptor["artifact"]["created_at"] = "2026-10-07T10:03:30Z"
        self.assertIs(bind_build_envelope(envelope, descriptor=descriptor, plan=ci_plan()), envelope)
        self.assertEqual(canonical_json(envelope), before)

    def test_preupload_export_rejects_transport_metadata_and_old_draft_shape(self):
        for key, value in (("upload_window", ci_descriptor()["producer"]["upload_window"]),
                           ("artifact_id", 100), ("digest", "sha256:" + "f" * 64), ("size", 1)):
            envelope = ci_envelope()
            envelope["producer"][key] = value
            with self.subTest(key=key), self.assertRaises(MbError):
                validate_build_envelope(envelope, plan=ci_plan())

    def test_selected_descriptor_keeps_required_actual_window(self):
        descriptor = ci_descriptor()
        descriptor["producer"].pop("upload_window")
        with self.assertRaises(MbError):
            bind_build_envelope(ci_envelope(), descriptor=descriptor, plan=ci_plan())

    def test_every_preupload_producer_field_remains_bound(self):
        for key, value in (("run_id", 43), ("run_attempt", 3), ("api_head_sha", "f" * 40),
                           ("graph_sha256", "f" * 64), ("workflow_path", ".github/workflows/other.yml"),
                           ("workflow_ref", "example/mod/.github/workflows/other.yml@refs/heads/master"),
                           ("event", "push")):
            envelope = ci_envelope()
            envelope["producer"][key] = value
            with self.subTest(key=key), self.assertRaises(MbError):
                bind_build_envelope(envelope, descriptor=ci_descriptor(), plan=ci_plan())

    def test_descriptor_artifact_scope_and_target_are_independently_bound(self):
        for kind, unit in (("target", "target-a"), ("runtime", "lane-a"), ("tested", "build")):
            with self.subTest(kind=kind), self.assertRaises(MbError):
                bind_build_envelope(ci_envelope(), descriptor=ci_descriptor(kind, unit_id=unit), plan=ci_plan())

    def test_exact_complete_and_target_output_unions(self):
        envelope = ci_envelope()
        self.assertIs(validate_build_envelope(envelope, plan=ci_plan()), envelope)
        envelope.update(scope="target", target_id="target-a")
        validate_build_envelope(envelope, plan=ci_plan())

    def test_missing_extra_duplicate_and_native_report_inventory_are_rejected(self):
        mutations = [lambda d: d["files"].pop(), lambda d: d["files"].append(copy.deepcopy(d["files"][0])),
                     lambda d: d["files"][0].update(lane_id="other"), lambda d: d["native_reports"].clear(),
                     lambda d: d["files"].reverse(), lambda d: d.update(target_id="target-a"),
                     lambda d: d["files"][0].update(path="../escape"),
                     lambda d: d["files"][0].update(path=grammar.CI_ENVELOPE_NAME),
                     lambda d: d["files"][0].update(size=limits.MAX_CI_EXPORT_FILE_BYTES + 1),
                     lambda d: d.update(plan_sha256="f" * 64)]
        for index, mutate in enumerate(mutations):
            envelope = ci_envelope()
            mutate(envelope)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_build_envelope(envelope, plan=ci_plan())

    def test_native_file_role_caps_are_preserved(self):
        for role, bound in (("production", limits.MAX_CI_JAR_BYTES), ("harness", limits.MAX_CI_JAR_BYTES),
                            ("native-report", limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE["block-pops"])):
            envelope = ci_envelope()
            file = next(item for item in envelope["files"] if item["role"] == role)
            file["size"] = bound
            validate_build_envelope(envelope)
            file["size"] += 1
            with self.assertRaises(MbError):
                validate_build_envelope(envelope)

    def test_block_pops_original_eight_mib_report_does_not_change_quick_skin_bound(self):
        for profile, bound in (("block-pops", 8 * 1024 * 1024), ("quick-skin", 4 * 1024 * 1024)):
            for scope in ("complete", "target"):
                plan, envelope = ci_plan(), ci_envelope()
                plan["profile"] = envelope["profile"] = profile
                plan["plan_sha256"] = envelope["plan_sha256"] = plan_sha256(plan)
                envelope.update(scope=scope, target_id="target-a" if scope == "target" else None)
                report = next(file for file in envelope["files"] if file["role"] == "native-report")
                report["size"] = bound
                with self.subTest(profile=profile, scope=scope):
                    validate_build_envelope(envelope, plan=plan)
                    report["size"] += 1
                    with self.assertRaises(MbError):
                        validate_build_envelope(envelope, plan=plan)

    def test_multiple_native_witnesses_and_build_logs_are_retained(self):
        plan, envelope = ci_plan(), ci_envelope()
        for role, path in (("native-report", "staged/lane-a/jdk-probe.json"),
                           ("native-report", "staged/lane-a/task-observations.json"),
                           ("build-log", "staged/lane-a/compiler.log")):
            output = {"lane_id": "lane-a", "role": role, "path": path}
            plan["targets"][0]["outputs"].append(output)
            envelope["files"].append({**output, "sha256": "a" * 64, "size": 128})
        plan["plan_sha256"] = plan_sha256(plan)
        validate_plan(plan)
        envelope["plan_sha256"] = plan["plan_sha256"]
        envelope["files"].sort(key=lambda item: item["path"])
        envelope["native_reports"] = [item["path"] for item in envelope["files"] if item["role"] == "native-report"]
        validate_build_envelope(envelope, plan=plan)
        log = next(item for item in envelope["files"] if item["role"] == "build-log")
        log["size"] = limits.MAX_CI_LOG_BYTES + 1
        with self.assertRaises(MbError):
            validate_build_envelope(envelope, plan=plan)

    def test_path_aliases_and_file_directory_conflicts_reject_before_extraction(self):
        for paths in (("a", "A"), ("Dir/a", "dir/b"), ("a", "a/b")):
            envelope = ci_envelope()
            envelope["files"] = sorted([{**envelope["files"][0], "path": p, "role": "native-report"}
                                        for p in paths], key=lambda item: item["path"])
            envelope["native_reports"] = sorted(paths)
            with self.assertRaises(MbError):
                validate_build_envelope(envelope)


class GateTests(unittest.TestCase):
    def test_both_complete_gates_match_the_admitted_plan(self):
        for gate in ("build", "packaged"):
            validate_gate_receipt(ci_gate(gate), plan=ci_plan())

    def test_independent_upload_windows_remain_bound_to_the_same_run_attempt(self):
        gate = ci_gate("packaged")
        lane = gate["artifacts"][0]
        lane["producer"]["upload_window"] = {"started_at": "2026-10-07T09:00:00Z", "completed_at": "2026-10-07T09:01:00Z"}
        lane["artifact"]["created_at"] = "2026-10-07T09:00:30Z"
        validate_gate_receipt(gate, plan=ci_plan())

    def test_partial_mixed_attempt_duplicate_and_wrong_owning_build_reject(self):
        mutations = [lambda d: d["artifacts"].pop(0), lambda d: d["artifacts"].append(copy.deepcopy(d["artifacts"][0])),
                     lambda d: d["native_receipts"].clear(), lambda d: d.update(mode="deferred"),
                     lambda d: d["native_receipts"][0].update(native_contract_sha256="f" * 64),
                     lambda d: d["owning_build"]["identity"].update(head_sha="f" * 40, tested_parents=["2" * 40, "f" * 40]),
                     lambda d: d["artifacts"][0]["producer"].update(graph_sha256="f" * 64),
                     lambda d: d["artifacts"][0]["artifact"].update(id=100)]
        for index, mutate in enumerate(mutations):
            gate = ci_gate("packaged")
            mutate(gate)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_gate_receipt(gate, plan=ci_plan())
        gate = ci_gate("packaged")
        gate["artifacts"][0]["producer"]["run_attempt"] = 3
        gate["artifacts"][0]["artifact"]["name"] = grammar.ci_artifact_name("runtime", 43, 3, "lane-a")
        with self.assertRaises(MbError):
            validate_gate_receipt(gate, plan=ci_plan())


class SelectionAndReuseTests(unittest.TestCase):
    def test_final_original_test_merge_round_trips_with_separate_provenance(self):
        reference = same_commit_reuse()
        original = canonical_json(reference["source"])
        plan = ci_plan()
        plan["identity"] = copy.deepcopy(reference["identity"])
        plan["plan_sha256"] = plan_sha256(plan)
        reference["plan_sha256"] = plan["plan_sha256"]
        bind_reuse_reference(reference, descriptor=selected_record(reference), plan=plan)
        self.assertEqual(load_document(canonical_json(reference), kind="mod-base.ci.reuse"), reference)
        self.assertEqual(canonical_json(reference["source"]), original)
        self.assertEqual(reference["identity"]["tested_sha"], reference["source"]["identity"]["tested_sha"])
        self.assertEqual(reference["identity"]["pr_number"], 0)
        self.assertGreater(reference["source"]["identity"]["pr_number"], 0)
        self.assertNotEqual(reference["identity"]["controller_sha"], reference["source"]["identity"]["controller_sha"])
        self.assertNotEqual(reference["producer"]["run_id"], reference["source"]["build_seal"]["producer"]["run_id"])

    def test_actual_merged_identity_reader_and_reference_agree_on_same_commit(self):
        reference = same_commit_reuse()
        plan, api, pr = protocol_fixtures.seeded_pr()
        original = plan["identity"]
        merged = original["tested_sha"]
        pr.update(state="closed", merged=True, merged_at="2026-10-08T10:00:00Z", merge_commit_sha=merged)
        pr["base"]["sha"] = merged
        api.add_response(f'/repos/{api.repository}/pulls/7', pr)
        api.set_branch("master", merged, original["tested_tree"])
        api.add_compare(original["base_sha"], merged, {"status": "ahead", "ahead_by": 2, "behind_by": 0})
        api.add_compare(merged, merged, {"status": "identical", "ahead_by": 0, "behind_by": 0})
        observation = authenticate_merged_pr_identity(api, original, controller_sha=merged, merged_sha=merged)
        self.assertEqual(reference["source"]["identity"], original)
        self.assertEqual(reference["identity"]["tested_sha"], observation.merged_sha)
        self.assertEqual(reference["identity"]["tested_tree"], observation.merged_tree)
        self.assertEqual(reference["identity"]["tested_parents"], list(observation.merged_parents))
        bind_reuse_reference(reference, descriptor=selected_record(reference))
        self.assertEqual(api.mutations, [])

    def test_same_commit_still_requires_original_pr_and_coherent_direct_seals(self):
        changes = [lambda d: d["identity"].update(pr_number=7),
                   lambda d: d["source"]["identity"].update(pr_number=0),
                   lambda d: d["source"]["identity"]["tested_parents"].reverse(),
                   lambda d: d["identity"].update(tested_tree="f" * 40),
                   lambda d: d["identity"].update(policy_sha256="f" * 64),
                   lambda d: d["identity"]["kit"].update(sha="f" * 40),
                   lambda d: d["source"]["build_seal"]["artifact"].update(name=grammar.ci_artifact_name("reuse", 42, 2)),
                   lambda d: d["source"]["packaged_seal"].update(plan_sha256="f" * 64),
                   lambda d: d["source"]["packaged_seal"]["artifact"].update(id=100),
                   lambda d: d["producer"].update(event="pull_request_target")]
        for index, change in enumerate(changes):
            reference = same_commit_reuse()
            change(reference)
            with self.subTest(index=index), self.assertRaises(MbError):
                bind_reuse_reference(reference, descriptor=selected_record(reference))

    def test_request_and_bundle_bind_the_entire_plan(self):
        selection = ci_selection()
        validate_source_selection(selection, plan=ci_plan())
        selection["build"]["identity"]["runtime_selection_sha256"] = "f" * 64
        with self.assertRaises(MbError):
            validate_source_selection(selection, plan=ci_plan())

    def test_partition_cannot_replace_a_complete_build_input(self):
        selection = ci_selection()
        selection["build"] = ci_descriptor("target", unit_id="target-a")
        with self.assertRaises(MbError):
            validate_source_selection(selection)

    def test_reuse_retains_distinct_covered_and_tested_identities(self):
        reference = ci_reuse()
        validate_reuse_reference(reference)
        self.assertNotEqual(reference["identity"]["controller_sha"], reference["source"]["identity"]["controller_sha"])
        self.assertNotEqual(reference["identity"]["tested_sha"], reference["source"]["identity"]["tested_sha"])

    def test_chains_tree_policy_pin_or_mixed_sources_reject(self):
        mutations = [lambda d: d["source"]["build_seal"]["artifact"].update(name=grammar.ci_artifact_name("reuse", 42, 2)),
                     lambda d: d["identity"].update(tested_tree="f" * 40),
                     lambda d: d["identity"].update(policy_sha256="f" * 64),
                     lambda d: d["identity"]["kit"].update(sha="f" * 40),
                     lambda d: d["source"]["packaged_seal"].update(plan_sha256="f" * 64),
                     lambda d: d["identity"].update(head_branch="feature"),
                     lambda d: d["source"]["packaged_seal"]["artifact"].update(id=100)]
        for index, mutate in enumerate(mutations):
            reference = ci_reuse()
            mutate(reference)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_reuse_reference(reference)

    def test_all_record_kinds_have_strict_json_and_unknown_key_rejection(self):
        for document in (ci_envelope(), ci_selection(), ci_gate(), ci_gate("packaged"), ci_reuse()):
            kind = document["kind"]
            self.assertEqual(load_document(canonical_json(document), kind=kind), document)
            with self.assertRaises(MbError):
                load_document(canonical_json(document).replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'), kind=kind)
            document["authority"] = "success"
            with self.assertRaises(MbError):
                validate_document(document)


if __name__ == "__main__":
    unittest.main()
