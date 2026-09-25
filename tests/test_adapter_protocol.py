from __future__ import annotations

import ast
import copy
import re
import unittest
from pathlib import Path

from mod_base import ADAPTER_API
from mod_base.adapter import protocol
from mod_base.adapter.protocol import HookFailed, HookUnsupported
from mod_base.errors import MbError
from mod_base.model.validators import DocumentError
from tests.helpers import load_fixture, promotion

SHA = "a" * 40
H64 = "b" * 64


def target() -> dict:
    expectation = load_fixture("valid/expectation.json")
    return {"key": expectation["key"], "label": expectation["label"], "subject": expectation["subject"],
            "matrix_sha256": expectation["matrix_sha256"], "contract_sha256": expectation["contract_sha256"]}


def collect_result() -> dict:
    expectation = load_fixture("valid/expectation.json")
    frames = []
    files = []
    for capture in expectation["captures"]:
        path = f"profiles/{capture['lane_id'].split('/')[0]}/{capture['step']}.png"
        files.append(path)
        frames.append({"frame_id": capture["frame_id"], "source_path": path, "runtime_evidence": "PASS"})
    return {
        "runtime_files": sorted(files + ["profiles/result.json"]),
        "lanes": [{"lane_id": lane["lane_id"], "java": lane["java"], "profile": "pr", "status": "pass",
                   "jars": {"production_sha256": H64}} for lane in expectation["lanes"]],
        "frames": frames,
        "comparisons": [{"comparison_id": item["comparison_id"]} for item in expectation["comparisons"]],
    }


def request(hook: str, arguments: dict, *, network: bool = False) -> dict:
    return {
        "kind": protocol.REQUEST_KIND, "schema_version": 1, "api": ADAPTER_API, "hook": hook,
        "context": {"repo_root": "/work/mod", "config_path": "/work/mod/site/mod-base.json", "kit_src": "/work/kit/src",
                    "tmpdir": "/tmp/mb-1", "implementation_sha": SHA, "network": network},
        "arguments": arguments,
    }


def response(hook: str, **fields) -> dict:
    return {"kind": protocol.RESPONSE_KIND, "schema_version": 1, "hook": hook, **fields}


class HookSetTest(unittest.TestCase):
    def test_hook_catalog_matches_the_spec(self) -> None:
        self.assertEqual(protocol.HOOKS, frozenset({
            "targets", "expectation", "collect", "expected_source_jobs", "authenticate_extensions", "compose",
            "verify_publication", "family_validate", "anchor_selection"}))
        self.assertEqual(protocol.NETWORK_HOOKS, {"authenticate_extensions", "compose", "verify_publication"})
        self.assertEqual(protocol.TOKEN_JOBS, {"admit", "collect", "family", "build"})
        self.assertEqual(protocol.FIXTURE_HOOKS, {"synthesize"})
        self.assertEqual(protocol.ADAPTER_API_WINDOW, frozenset({1}))
        self.assertEqual(set(protocol.HOOK_JOBS), protocol.HOOKS)
        self.assertEqual(set(protocol.ARGUMENTS), protocol.HOOKS)
        self.assertEqual(set(protocol.RESULTS), protocol.HOOKS)

    def test_where_hooks_may_run(self) -> None:
        for hook, jobs in protocol.HOOK_JOBS.items():
            with self.subTest(hook=hook):
                self.assertFalse(jobs & protocol.FORBIDDEN_JOBS)
                if hook in protocol.NETWORK_HOOKS:
                    self.assertLessEqual(jobs, protocol.TOKEN_JOBS)
        self.assertEqual(protocol.HOOK_JOBS["family_validate"], {"family"})
        self.assertEqual(protocol.HOOK_JOBS["compose"], {"collect"})
        self.assertEqual(protocol.HOOK_JOBS["verify_publication"], {"build"})
        self.assertIn(protocol.PREPARE_EVIDENCE, protocol.HOOK_JOBS["anchor_selection"])


class RequestEnvelopeTest(unittest.TestCase):
    def test_valid_requests(self) -> None:
        protocol.validate_request(request("targets", {"branches": None}))
        protocol.validate_request(request("targets", {"branches": [{"name": "master", "commit": SHA, "tree": SHA}]}))
        protocol.validate_request(request("expectation", {"target": target(), "tested_run": {"event": "schedule",
                                                                                            "branch": "master"},
                                                          "extensions": {"block-pops.aggregate_scope": {"kind": "legacy"}}}))
        protocol.validate_request(request("compose", {"key": "mc1.20.1", "selected_compact_dir": "/tmp/a",
                                                      "output_dir": "/tmp/b"}, network=True))
        protocol.validate_request(request("anchor_selection", {"expectation": load_fixture("valid/expectation.json")}))
        draft = {key: value for key, value in promotion().items() if key != "site"}
        protocol.validate_request(request("verify_publication", {"promotion_draft": draft}, network=True))

    def test_invalid_requests(self) -> None:
        cases = [
            request("targets", {"branches": None}, network=True),
            request("targets", {}),
            {**request("targets", {"branches": None}), "api": 2},
            {**request("targets", {"branches": None}), "hook": "synthesize"},
            {**request("targets", {"branches": None}), "extra": 1},
            request("compose", {"key": "mc1.20.1", "selected_compact_dir": "relative", "output_dir": "/tmp/b"}),
            request("compose", {"key": "mc1.20.1", "selected_compact_dir": "/tmp/../etc", "output_dir": "/tmp/b"}),
            request("compose", {"key": "mc1.20.1", "selected_compact_dir": "/tmp//a", "output_dir": "/tmp/b/"}),
            request("family_validate", {"family": "mod-compatibility", "key": "mc1.20.1", "bundle_dir": "/a",
                                        "expected_coverage_sha": "x", "output_dir": "/b"}),
            request("expectation", {"target": {**target(), "extra": 1}, "tested_run": None, "extensions": {}}),
            request("expectation", {"target": target(), "tested_run": None, "extensions": {"Bad.Name": {}}}),
            request("verify_publication", {"promotion_draft": {"kind": "mod-base.promotion"}}),
            request("verify_publication", {"promotion_draft": promotion()}),
        ]
        for document in cases:
            with self.subTest(document=document.get("hook")), self.assertRaises(MbError):
                protocol.validate_request(document)


class ResponseEnvelopeTest(unittest.TestCase):
    def test_statuses(self) -> None:
        arguments = {"branches": None}
        result = [target()]
        self.assertEqual(protocol.validate_response(response("targets", status="ok", result=result), hook="targets",
                                                    arguments=arguments), result)
        with self.assertRaises(HookUnsupported):
            protocol.validate_response(response("targets", status="unsupported"), hook="targets", arguments=arguments)
        with self.assertRaisesRegex(HookFailed, "matrix unreadable"):
            protocol.validate_response(response("targets", status="error", error="matrix unreadable"), hook="targets",
                                       arguments=arguments)
        for bad in (
            response("targets", status="ok"),
            response("targets", status="error"),
            response("targets", status="unsupported", result=None),
            response("targets", status="ok", result=result, error="x"),
            response("collect", status="ok", result=result),
            {**response("targets", status="ok", result=result), "extra": 1},
            response("targets", status="error", error="line\nforged"),
        ):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                protocol.validate_response(bad, hook="targets", arguments=arguments)
        with self.assertRaises(MbError):
            protocol.validate_response(response("synthesize", status="ok", result=None), hook="synthesize",
                                       arguments={})


class ResultSchemaTest(unittest.TestCase):
    def test_targets(self) -> None:
        protocol.validate_result("targets", [target()], {"branches": None})
        other = copy.deepcopy(target())
        other["key"] = "mc26.3"
        other["subject"]["commit"] = "c" * 40
        with self.assertRaisesRegex(DocumentError, "same protected head"):
            protocol.validate_result("targets", [target(), other], {"branches": None})
        with self.assertRaisesRegex(DocumentError, "duplicates an earlier item"):
            protocol.validate_result("targets", [target(), target()], {"branches": None})
        heads = [{"name": "master", "commit": target()["subject"]["commit"], "tree": target()["subject"]["tree"]}]
        protocol.validate_result("targets", [target()], {"branches": heads})
        with self.assertRaisesRegex(DocumentError, "must be one of the listed branch heads"):
            protocol.validate_result("targets", [target()], {"branches": [{**heads[0], "tree": "d" * 40}]})
        with self.assertRaises(DocumentError):
            protocol.validate_result("targets", [], {"branches": None})

    def test_expectation_must_match_the_target(self) -> None:
        expectation = load_fixture("valid/expectation.json")
        arguments = {"target": target(), "tested_run": None, "extensions": {}}
        protocol.validate_result("expectation", expectation, arguments)
        with self.assertRaisesRegex(DocumentError, "must equal the requested target"):
            protocol.validate_result("expectation", {**expectation, "matrix_sha256": H64}, arguments)

    def test_collect(self) -> None:
        expectation = load_fixture("valid/expectation.json")
        arguments = {"runtime_root": "/tmp/runtime", "target": target(), "expectation": expectation}
        protocol.validate_result("collect", collect_result(), arguments)
        mutations = {
            "frame ids must equal": lambda result: result["frames"].reverse(),
            "must be a listed runtime .png file": lambda result: result["frames"][0].update(source_path="missing.png"),
            "must be a relative .json or .png path": lambda result: result["runtime_files"].insert(0, "a.sh"),
            "lane ids must equal": lambda result: result["lanes"].pop(),
            "must equal the expectation lane": lambda result: result["lanes"][0].update(java=21),
            "comparison ids must equal": lambda result: result["comparisons"].pop(),
            "must be non-blank text": lambda result: result["frames"][0].update(runtime_evidence="\n"),
            "must be 'pass'": lambda result: result["lanes"][0].update(status="fail"),
            "strictly ascending": lambda result: result["runtime_files"].append("a.json"),
        }
        for message, mutate in mutations.items():
            result = collect_result()
            mutate(result)
            with self.subTest(message=message), self.assertRaisesRegex(DocumentError, message):
                protocol.validate_result("collect", result, arguments)
        reported = collect_result()
        reported["frames"][0]["reported_pixel"] = load_fixture("valid/handoff.json")["frames"][0]["source"]["pixel"]
        protocol.validate_result("collect", reported, arguments)

    def test_expected_source_jobs(self) -> None:
        arguments = {}
        self.assertIsNone(protocol.validate_result("expected_source_jobs", None, arguments))
        jobs = [{"name": "Packaged E2E gate", "conclusion": "success"}, {"name": "Build", "conclusion": "skipped"}]
        protocol.validate_result("expected_source_jobs", jobs, arguments)
        for bad in ([], jobs + jobs[:1], [{"name": "x", "conclusion": "done"}], [{"name": "x"}]):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                protocol.validate_result("expected_source_jobs", bad, arguments)

    def test_authenticate_extensions(self) -> None:
        arguments = {"manifest": {}, "extensions": {"quick-skin.runtime_source": {}}}
        protocol.validate_result("authenticate_extensions",
                                 {"verified": ["quick-skin.runtime_source"], "reuse_verified": True}, arguments)
        for bad in ({"verified": [], "reuse_verified": True}, {"verified": ["quick-skin.runtime_source"]},
                    {"verified": ["quick-skin.runtime_source", "quick-skin.x"], "reuse_verified": False}):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                protocol.validate_result("authenticate_extensions", bad, arguments)

    def test_compose(self) -> None:
        arguments = {"key": "mc1.20.1"}
        good = {"baseline_artifact": {"id": 5, "name": f"mb-baseline--mc1.20.1--{SHA}--9", "digest": "sha256:" + H64}}
        protocol.validate_result("compose", good, arguments)
        for name in (f"mb-baseline--mc26.3--{SHA}--9", f"mb-cache--mc1.20.1--{SHA}", "pages-full-baseline-x"):
            bad = copy.deepcopy(good)
            bad["baseline_artifact"]["name"] = name
            with self.subTest(name=name), self.assertRaises(DocumentError):
                protocol.validate_result("compose", bad, arguments)

    def test_verify_publication(self) -> None:
        self.assertIsNone(protocol.validate_result("verify_publication", None, {}))
        with self.assertRaises(DocumentError):
            protocol.validate_result("verify_publication", True, {})

    def test_family_validate(self) -> None:
        arguments = {"expected_coverage_sha": SHA}
        protocol.validate_result("family_validate", {"status": "available", "reason": "clean wave",
                                                     "projection_path": "paired.json", "carried_from": "c" * 40,
                                                     "impact_paths_sha256": H64}, arguments)
        protocol.validate_result("family_validate", {"status": "superseded", "reason": "contract drift"}, arguments)
        for bad in (
            {"status": "available", "reason": "x"},
            {"status": "unavailable", "reason": "x", "projection_path": "paired.json"},
            {"status": "unavailable", "reason": "x", "carried_from": "c" * 40},
            {"status": "available", "reason": "x", "projection_path": "../paired.json"},
            {"status": "available", "reason": "x", "projection_path": "paired.json", "carried_from": SHA},
            {"status": "stale", "reason": "x"},
            {"status": "superseded", "reason": "a" * 201},
        ):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                protocol.validate_result("family_validate", bad, arguments)

    def test_anchor_selection(self) -> None:
        expectation = load_fixture("valid/expectation.json")
        arguments = {"expectation": expectation}
        self.assertIsNone(protocol.validate_result("anchor_selection", None, arguments))
        protocol.validate_result("anchor_selection", {"artifact_nodes": ["1.20.1-fabric", "1.20.1-forge"]}, arguments)
        for bad in ({"artifact_nodes": []}, {"artifact_nodes": ["26.3-fabric"]},
                    {"artifact_nodes": ["1.20.1-forge", "1.20.1-fabric"]}, {"artifact_nodes": ["1.20.1-fabric"]}):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                protocol.validate_result("anchor_selection", bad, arguments)
        undeclared = {"expectation": {**expectation, "anchor": None}}
        self.assertIsNone(protocol.validate_result("anchor_selection", None, undeclared))
        with self.assertRaisesRegex(DocumentError, "must equal the expectation's declared anchor"):
            protocol.validate_result("anchor_selection", {"artifact_nodes": ["1.20.1-fabric", "1.20.1-forge"]},
                                     undeclared)

    def test_fixture_hooks_are_separate_from_pages_hooks(self) -> None:
        arguments = {"target": target(), "expectation": load_fixture("valid/expectation.json"), "out_root": "/tmp/out"}
        protocol.validate_fixture_arguments("synthesize", arguments)
        protocol.validate_fixture_result("synthesize", None)
        with self.assertRaises(DocumentError):
            protocol.validate_fixture_result("synthesize", {"files": 1})
        with self.assertRaises(DocumentError):
            protocol.validate_fixture_arguments("synthesize", {**arguments, "out_root": "relative"})
        with self.assertRaises(DocumentError):
            protocol.validate_fixture_arguments("synthesize", {**arguments, "image_factory": "x"})
        for hook in ("targets", "evil"):
            with self.subTest(hook=hook), self.assertRaises(MbError):
                protocol.validate_fixture_arguments(hook, {})
        with self.assertRaises(MbError):
            protocol.validate_arguments("synthesize", arguments)
        self.assertFalse(protocol.FIXTURE_HOOKS & protocol.HOOKS)
        self.assertEqual(set(protocol.FIXTURE_ARGUMENTS), protocol.FIXTURE_HOOKS)

    def test_unknown_hook(self) -> None:
        with self.assertRaises(MbError):
            protocol.validate_result("synthesize", None, {})
        with self.assertRaises(MbError):
            protocol.validate_arguments("evil", {})


ADAPTER_DOC = Path(__file__).resolve().parents[1] / "docs" / "ADAPTER.md"


def doc_section(heading: str) -> str:
    """The text of the ADAPTER.md section ``heading`` (a ``##``/``###`` line), up to the next heading."""

    text = ADAPTER_DOC.read_text(encoding="utf-8")
    match = re.search(rf"^#+ {re.escape(heading)}\n(.*?)(?=^#+ |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"ADAPTER.md has no section {heading!r}")
    return match.group(1)


def table_rows(section: str) -> dict[str, str]:
    """``{first cell's code name: second cell}`` of every table row of ``section``."""

    return {match.group(1): match.group(2).strip()
            for match in re.finditer(r"^\| `([\w-]+)` \| (.*?) \|", section, flags=re.MULTILINE)}


class ConformanceContractDocumentTest(unittest.TestCase):
    """ADAPTER.md documents the optional conformance fixtures, the variants and the report exactly as
    the simulation uses them, so an adapter author (QS1, BP1) can make ``--families``, ``delegated``
    and ``selected`` run instead of being skipped (review finding: the contract was undocumented)."""

    @classmethod
    def setUpClass(cls) -> None:
        from mod_base.conformance import _simulation

        cls.simulation = _simulation
        cls.source = ast.parse(Path(_simulation.__file__).read_text(encoding="utf-8"))
        cls.fixtures = table_rows(doc_section("Optional conformance fixtures"))

    def keywords_passed(self, constant: str) -> set[tuple[str, ...]]:
        """The keyword names of every call that passes the fixture name ``constant`` with keywords."""

        calls = set()
        for node in ast.walk(self.source):
            if (isinstance(node, ast.Call) and node.keywords
                    and any(isinstance(argument, ast.Name) and argument.id == constant for argument in node.args)):
                calls.add(tuple(sorted(keyword.arg for keyword in node.keywords if keyword.arg is not None)))
        return calls

    def test_every_optional_fixture_is_documented_with_its_arguments(self) -> None:
        for constant in ("FAMILY_BUNDLE", "DELEGATED_EXTENSIONS", "SELECTED_EXTENSIONS"):
            name = getattr(self.simulation, constant)
            with self.subTest(fixture=name):
                self.assertIn(name, self.fixtures)
                signature = re.fullmatch(rf"`{name}\(ctx, ([\w, ]+)\)`", self.fixtures[name])
                self.assertIsNotNone(signature, f"ADAPTER.md must give {name}'s signature")
                documented = tuple(sorted(signature.group(1).split(", ")))
                self.assertEqual({documented}, self.keywords_passed(constant))

    def test_family_outcomes_and_the_responses_bound_are_documented(self) -> None:
        section = doc_section("Optional conformance fixtures")
        self.assertIn(self.simulation.FAMILY_OUTCOMES, self.fixtures)
        for outcome in self.simulation.FAMILY_OUTCOME_VALUES:
            self.assertIn(f"`{outcome}`", self.fixtures[self.simulation.FAMILY_OUTCOMES])
        self.assertIn(f"(at most {self.simulation.MAX_FIXTURE_RESPONSES})", section)
        self.assertIn("/repos/<repository>/", section)

    def test_the_variants_are_documented(self) -> None:
        variants = set()
        for node in ast.walk(self.source):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "variant"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                variants.add(node.args[0].value)
        self.assertGreaterEqual(len(variants), 5)
        conformance = doc_section("Conformance (in-process, no `sys.path` edits)")
        self.assertEqual(variants, set(table_rows(conformance)))
        report = table_rows(doc_section("The conformance report"))
        for variant in variants:
            self.assertIn(variant, report["variants"])

    def test_the_documented_report_keys_are_exactly_what_the_parent_accepts(self) -> None:
        from mod_base.conformance import run

        keys = set(table_rows(doc_section("The conformance report")))
        values = {"repository": "o/r", "kit": {}, "keys": [{}], "families": [], "checks": 1, "variants": {},
                  "admission": [], "hooks": [], "site": {}}
        self.assertEqual(keys, set(values))
        self.assertEqual(run._check_report(dict(values)), values)
        for key in sorted(keys):
            with self.subTest(missing=key), self.assertRaises(MbError):
                run._check_report({name: value for name, value in values.items() if name != key})
        with self.assertRaises(MbError):
            run._check_report({**values, "extra": 1})


if __name__ == "__main__":
    unittest.main()
