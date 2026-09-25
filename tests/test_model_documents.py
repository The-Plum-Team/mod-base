from __future__ import annotations

import unittest
import copy
from itertools import product

from mod_base import SCHEMA_VERSIONS, readable_schema_versions
from mod_base.errors import MbError
from mod_base.model import documents
from mod_base.model.canonical import StrictJsonError, canonical_json
from mod_base.model.documents import (
    VALIDATORS,
    check_compact_selection,
    compact_identity_sha256,
    inventory_sha256,
    load_document,
    run_claim_from_environment,
    thumbnail_size,
    validate_anchor,
    validate_compact,
    validate_document,
    validate_expectation,
    validate_gallery,
    validate_handoff,
    validate_promotion,
    validate_selection,
)
from mod_base.model.validators import DocumentError
from tests.helpers import (
    DOCUMENT_FIXTURES,
    VALID_FIXTURE_KINDS,
    apply_mutation,
    compact,
    embedded_selection,
    load_fixture,
    pretty_json,
    promotion,
    sample_documents,
    selection_draft,
)

def _context(case: dict) -> dict:
    return {name: load_fixture(f"valid/{fixture}.json") for name, fixture in case.get("context", {}).items()}


class ValidFixturesTest(unittest.TestCase):
    def test_every_kind_has_a_validator(self) -> None:
        self.assertEqual(set(VALIDATORS), set(SCHEMA_VERSIONS) - {"mod-base.config"})
        for kind in SCHEMA_VERSIONS:
            self.assertEqual(readable_schema_versions(kind), frozenset({1}))

    def test_every_kind_has_a_valid_fixture(self) -> None:
        self.assertEqual(set(VALID_FIXTURE_KINDS.values()), set(VALIDATORS))
        on_disk = {path.stem for path in (DOCUMENT_FIXTURES / "valid").glob("*.json")}
        self.assertEqual(on_disk, set(VALID_FIXTURE_KINDS))

    def test_committed_fixtures_equal_the_builders(self) -> None:
        for name, document in sample_documents().items():
            with self.subTest(name=name):
                self.assertEqual((DOCUMENT_FIXTURES / "valid" / f"{name}.json").read_bytes(), pretty_json(document))

    def test_valid_fixtures_validate(self) -> None:
        expectation = load_fixture("valid/expectation.json")
        for name, kind in VALID_FIXTURE_KINDS.items():
            document = load_fixture(f"valid/{name}.json")
            with self.subTest(name=name):
                self.assertIs(validate_document(document, kind=kind), document)
                if kind in {"mod-base.evidence.handoff", "mod-base.evidence.compact", "mod-base.evidence.anchor"}:
                    validate_document(document, kind=kind, expectation=expectation)

    def test_load_document_decodes_and_validates(self) -> None:
        data = canonical_json(load_fixture("valid/kit-stamp.json"))
        self.assertEqual(load_document(data, kind="mod-base.kit-stamp")["version"], "0.9.0")
        with self.assertRaises(MbError):
            load_document(data, kind="mod-base.unknown")
        with self.assertRaises(DocumentError):
            load_document(data, kind="mod-base.build")
        with self.assertRaises(StrictJsonError):
            load_document(data, kind="mod-base.kit-stamp", max_bytes=10)

    def test_raw_negative_fixtures_never_validate(self) -> None:
        for path in sorted((DOCUMENT_FIXTURES / "invalid" / "raw").iterdir()):
            with self.subTest(name=path.name), self.assertRaises(MbError) as caught:
                load_document(path.read_bytes(), kind="mod-base.kit-stamp")
            self.assertEqual(caught.exception.exit_code, 2)

    def test_dispatch_rejects_non_objects_and_unknown_kinds(self) -> None:
        for value in ([], "x", None, {"kind": "mod-base.config"}, {"kind": 1}, {}):
            with self.subTest(value=value), self.assertRaises(DocumentError):
                validate_document(value)


class MutationFixturesTest(unittest.TestCase):
    def test_mutation_cases(self) -> None:
        cases = load_fixture("invalid/mutations.json")["cases"]
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)))
        covered = set()
        for case in cases:
            document = apply_mutation(load_fixture(f"valid/{case['fixture']}.json"), case)
            kind = VALID_FIXTURE_KINDS[case["fixture"]]
            with self.subTest(case=case["name"]):
                if case["expect"] == "valid":
                    validate_document(document, kind=kind, **_context(case))
                else:
                    covered.add(kind)
                    with self.assertRaises(DocumentError) as caught:
                        validate_document(document, kind=kind, **_context(case))
                    self.assertIn(case["error"], str(caught.exception))
                    self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(covered, set(VALIDATORS), "every kind needs at least one negative case")

    def test_negative_categories_are_present(self) -> None:
        names = {case["name"] for case in load_fixture("invalid/mutations.json")["cases"]}
        for required in ("unknown-top-level-key", "unknown-nested-key", "fraction-above-one", "key-double-hyphen",
                         "runtime-evidence-4096", "runtime-evidence-4097", "runtime-evidence-newline",
                         "runtime-evidence-del", "runtime-evidence-blank", "bool-is-not-an-integer"):
            self.assertIn(required, names)

    def test_errors_name_the_exact_path(self) -> None:
        document = apply_mutation(load_fixture("valid/handoff.json"),
                                  {"set": [["/frames/3/source/pixel/meaningful_colors", 33]]})
        with self.assertRaises(DocumentError) as caught:
            validate_document(document)
        self.assertEqual(caught.exception.path, "$.frames[3].source.pixel.meaningful_colors")


class DraftDocumentsTest(unittest.TestCase):
    def test_selection_draft_lacks_exactly_the_completion_fields(self) -> None:
        draft = selection_draft()
        self.assertIs(validate_selection(draft, draft=True), draft)
        with self.assertRaisesRegex(DocumentError, "is missing required keys \\['binding', 'manifest_sha256'\\]"):
            validate_selection(draft)
        final = embedded_selection()
        with self.assertRaisesRegex(DocumentError, "has unknown keys"):
            validate_selection(final, draft=True)
        with self.assertRaisesRegex(DocumentError, "has unknown keys"):
            validate_selection({**draft, "composition": embedded_selection(composed=True)["composition"]}, draft=True)

    def test_draft_selection_keeps_every_run_rule(self) -> None:
        draft = apply_mutation(selection_draft(), {"set": [["/source/tested_run/commit", "f" * 40]]})
        with self.assertRaisesRegex(DocumentError, "tested branch and commit to be the subject's"):
            validate_selection(draft, draft=True)

    def test_promotion_draft_has_no_site(self) -> None:
        final = promotion()
        draft = {key: value for key, value in final.items() if key != "site"}
        self.assertIs(validate_promotion(draft, draft=True), draft)
        with self.assertRaisesRegex(DocumentError, "is missing required keys \\['site'\\]"):
            validate_promotion(draft)
        with self.assertRaisesRegex(DocumentError, "has unknown keys"):
            validate_promotion(final, draft=True)
        with self.assertRaisesRegex(DocumentError, "must map the implementation branch"):
            validate_promotion({**draft, "heads": {"master": "7" * 40}}, draft=True)


class CompactSelectionBindingTest(unittest.TestCase):
    def test_fixtures_are_bound(self) -> None:
        for composed in (False, True):
            with self.subTest(composed=composed):
                document, selection = compact(composed=composed), embedded_selection(composed=composed)
                validate_compact(document)
                validate_selection(selection)
                check_compact_selection(document, selection)

    def test_identity_ignores_only_the_embedded_selection(self) -> None:
        document = compact()
        identity = compact_identity_sha256(document)
        other_selection = apply_mutation(document, {"set": [["/selection/sha256", "e" * 64]]})
        other_selection["files"] = [dict(record, sha256="e" * 64) if record["path"] == "selection.json" else record
                                    for record in other_selection["files"]]
        self.assertEqual(compact_identity_sha256(other_selection), identity)
        changed = apply_mutation(document, {"set": [["/frames/0/runtime_evidence", "PASS another message"]]})
        self.assertNotEqual(compact_identity_sha256(changed), identity)
        with self.assertRaises(MbError):
            compact_identity_sha256({"files": []})

    def test_every_binding_field_is_checked(self) -> None:
        composed_selection = embedded_selection(composed=True)
        cases = {
            "manifest_sha256": (compact(), {"set": [["/manifest_sha256", "e" * 64]]}),
            "binding.frames": (compact(), {"set": [["/binding/frames", 3]]}),
            "binding.derivatives": (compact(), {"set": [["/binding/derivatives", 3]]}),
            "extensions_verified": (compact(), {"set": [["/extensions_verified", ["quick-skin.runtime_source"]]]}),
            "selected_artifact": (compact(), {"set": [["/selected_artifact/id", 9002]]}),
            "source.tested_run": (compact(), {"set": [["/source/tested_run/controller_branch", "other"]]}),
            "kit": (compact(), {"set": [["/kit/sha", "9" * 40]]}),
            "expectation_sha256": (compact(), {"set": [["/expectation_sha256", "e" * 64]]}),
            "composition": (compact(composed=True), {"delete": ["/composition"]}),
        }
        for field, (document, mutation) in cases.items():
            base = composed_selection if document["scope"]["kind"] == "composed" else embedded_selection()
            with self.subTest(field=field), self.assertRaisesRegex(DocumentError, f"selection.{field}"):
                check_compact_selection(document, apply_mutation(base, mutation))
        wrong_baseline = apply_mutation(composed_selection, {"set": [["/composition/baseline_artifact/id", 8002]]})
        with self.assertRaisesRegex(DocumentError, "must name the compact's scope.components.baseline"):
            check_compact_selection(compact(composed=True), wrong_baseline)
        with self.assertRaisesRegex(DocumentError, "is required exactly for a composed compact bundle"):
            check_compact_selection(compact(), apply_mutation(embedded_selection(), {
                "set": [["/composition", composed_selection["composition"]]]}))


def _selected_expectation() -> dict:
    expectation = load_fixture("valid/expectation.json")
    detail = {"modules": ["skin-import"]}
    from mod_base.model.canonical import canonical_sha256

    expectation["scope"] = {"kind": "selected", "detail": detail, "detail_sha256": canonical_sha256(detail)}
    return expectation


class IntermediateCompactionTest(unittest.TestCase):
    def test_selected_compaction_is_an_intermediate_only(self) -> None:
        expectation = _selected_expectation()
        document = compact()
        document["scope"] = {"kind": "selected", "detail_sha256": expectation["scope"]["detail_sha256"]}
        validate_compact(document, intermediate=True)
        validate_compact(document, expectation=expectation, intermediate=True)
        with self.assertRaisesRegex(DocumentError, "must be one of \\['complete', 'composed'\\]"):
            validate_compact(document)
        complete = {**expectation, "scope": {**expectation["scope"], "kind": "complete"}}
        validate_expectation(complete)
        with self.assertRaisesRegex(DocumentError, "embeds its selected expectation"):
            validate_compact(document, expectation=complete, intermediate=True)

    def test_published_bundles_embed_the_complete_expectation(self) -> None:
        expectation = _selected_expectation()
        composed = compact(composed=True)
        composed["scope"]["detail_sha256"] = expectation["scope"]["detail_sha256"]
        with self.assertRaisesRegex(DocumentError, "embeds the complete expectation"):
            validate_compact(composed, expectation=expectation)


class AnchorExpectationTest(unittest.TestCase):
    def test_anchor_requires_the_declared_anchor(self) -> None:
        anchor = load_fixture("valid/anchor.json")
        expectation = load_fixture("valid/expectation.json")
        validate_anchor(anchor, expectation=expectation)
        undeclared = {**expectation, "anchor": None}
        validate_expectation(undeclared)
        with self.assertRaisesRegex(DocumentError, "declares no anchor"):
            validate_anchor(anchor, expectation=undeclared)
        narrower = {**expectation, "anchor": {"artifact_nodes": ["1.20.1-fabric"]}}
        validate_expectation(narrower)
        with self.assertRaisesRegex(DocumentError, "must equal the expectation's declared anchor.artifact_nodes"):
            validate_anchor(anchor, expectation=narrower)


class LoneSurrogateTest(unittest.TestCase):
    def test_in_process_documents_with_lone_surrogates_are_rejections(self) -> None:
        handoff = load_fixture("valid/handoff.json")
        handoff["frames"][0]["runtime_evidence"] = "PASS \ud800"
        with self.assertRaisesRegex(DocumentError, "runtime_evidence: must be non-blank text"):
            validate_handoff(handoff)
        expectation = load_fixture("valid/expectation.json")
        expectation["scope"] = {"kind": "complete", "detail": {"note": "\udfff"}, "detail_sha256": "0" * 64}
        with self.assertRaises(DocumentError) as caught:
            validate_expectation(expectation)
        self.assertEqual(caught.exception.path, "$.scope.detail")
        self.assertEqual(caught.exception.exit_code, 2)
        expectation["scope"]["detail"] = {1: "x"}
        with self.assertRaisesRegex(DocumentError, "cannot be encoded as canonical JSON"):
            validate_expectation(expectation)


class GalleryFamilyReleasesTest(unittest.TestCase):
    """A family is one view with one release per key (Quick Skin publishes 17 keys)."""

    @staticmethod
    def gallery(keys: list[str]) -> dict:
        base = load_fixture("valid/gallery.json")
        document = copy.deepcopy(base)
        document["releases"], document["lanes"], document["frames"], document["comparisons"] = [], [], [], []
        family = document["families"][0]
        release_template, lane_template = family["releases"][0], family["lanes"][0]
        family["releases"], family["lanes"], family["not_applicable"] = [], [], []
        for key in keys:
            for field in ("releases", "lanes", "frames", "comparisons"):
                for item in base[field]:
                    item = copy.deepcopy(item)
                    item["key"] = key
                    if field == "frames":
                        item["image"] = f"images/{key}/{item['published']['file_sha256']}.webp"
                    document[field].append(item)
            links = [{"label": f"Run {index}", "run_url": f"{base['project']['repository_url']}/actions/runs/{index + 1}"}
                     for index in range(16)]
            family["releases"].append({**copy.deepcopy(release_template), "key": key, "links": links})
            family["lanes"].append({**copy.deepcopy(lane_template), "key": key})
        return document

    def test_seventeen_keys_with_full_provenance(self) -> None:
        keys = [f"mc1.{minor}" for minor in range(10, 27)]
        validate_gallery(self.gallery(keys))

    def test_mixed_availability_per_key(self) -> None:
        document = self.gallery(["mc1.20.1", "mc26.3"])
        family = document["families"][0]
        family["releases"][1] = {"key": "mc26.3", "available": False, "status": "superseded"}
        family["lanes"] = family["lanes"][:1]
        validate_gallery(document)
        family["available"], family["status"] = False, "superseded"
        with self.assertRaisesRegex(DocumentError, "must summarize its releases"):
            validate_gallery(document)
        family["available"], family["status"] = True, "available"
        family["lanes"].append({**copy.deepcopy(family["lanes"][0]), "key": "mc26.3"})
        with self.assertRaisesRegex(DocumentError, "must be an available release key of this family"):
            validate_gallery(document)

    def test_uniqueness_is_scoped_by_key(self) -> None:
        document = self.gallery(["mc1.20.1", "mc26.3"])
        validate_gallery(document)
        family = document["families"][0]
        family["lanes"].append(copy.deepcopy(family["lanes"][0]))
        with self.assertRaisesRegex(DocumentError, "duplicates lane_id"):
            validate_gallery(document)


class PureHelpersTest(unittest.TestCase):
    def test_thumbnail_size_matches_pillow(self) -> None:
        from PIL import Image

        sources = [(1920, 1080), (1600, 900), (1280, 720), (1000, 1000), (640, 360), (333, 777), (7, 3), (1, 1)]
        boxes = [(1600, 900), (1280, 720), (500, 500), (100, 30), (2000, 2000), (1, 1)]
        for source, box in product(sources, boxes):
            image = Image.new("RGB", source)
            image.thumbnail(box)
            with self.subTest(source=source, box=box):
                self.assertEqual(thumbnail_size(source, box), image.size)

    def test_thumbnail_size_rejects_non_positive(self) -> None:
        with self.assertRaises(MbError):
            thumbnail_size((0, 10), (5, 5))

    def test_inventory_sha256_is_order_independent_and_rejects_duplicates(self) -> None:
        records = [{"path": "b", "sha256": "1" * 64, "size": 1}, {"path": "a", "sha256": "2" * 64, "size": 2}]
        self.assertEqual(inventory_sha256(records), inventory_sha256(list(reversed(records))))
        self.assertNotEqual(inventory_sha256(records), inventory_sha256(records[:1]))
        with self.assertRaises(MbError):
            inventory_sha256(records + records[:1])

    def test_https_urls(self) -> None:
        for good in ("https://modrinth.com/mod/quick-skin", "https://www.curseforge.com/minecraft/mc-mods/quick-skin",
                     "https://github.com/o/r/issues?q=a#x"):
            self.assertTrue(documents.is_https_url(good), good)
        for bad in ("http://a.com", "https://a", "https://user@a.com", "https://a.com:443/", "https://A.com",
                    "https://a.com/ x", "https://a.com/é", "https://.a.com", "https://a..com", "javascript:x",
                    'https://a.com/"', "https://a.com/<x>"):
            with self.subTest(bad=bad):
                self.assertFalse(documents.is_https_url(bad))


class RunClaimFromEnvironmentTest(unittest.TestCase):
    ENVIRONMENT = {
        "GITHUB_REPOSITORY": "The-Plum-Team/Quick-Skin-Mod",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF_NAME": "master",
        "GITHUB_WORKFLOW_REF": "The-Plum-Team/Quick-Skin-Mod/.github/workflows/on-demand-e2e.yml@refs/heads/master",
    }

    def test_builds_a_self_controlled_claim(self) -> None:
        self.assertEqual(run_claim_from_environment(self.ENVIRONMENT), {
            "run_id": 123, "run_attempt": 2, "workflow_path": ".github/workflows/on-demand-e2e.yml",
            "branch": "master", "commit": "a" * 40, "controller_branch": "master", "controller_sha": "a" * 40,
        })

    def test_rejects_inconsistent_or_missing_environment(self) -> None:
        mutations = [
            {"GITHUB_RUN_ID": "0"}, {"GITHUB_RUN_ATTEMPT": "1x"}, {"GITHUB_SHA": "A" * 40},
            {"GITHUB_REF_NAME": "other"},
            {"GITHUB_WORKFLOW_REF": "Other/Repo/.github/workflows/on-demand-e2e.yml@refs/heads/master"},
            {"GITHUB_WORKFLOW_REF": "The-Plum-Team/Quick-Skin-Mod/.github/workflows/x.yml@refs/tags/v1"},
            {"GITHUB_REPOSITORY": ""},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                run_claim_from_environment({**self.ENVIRONMENT, **mutation})
        environment = dict(self.ENVIRONMENT)
        del environment["GITHUB_SHA"]
        with self.assertRaises(MbError):
            run_claim_from_environment(environment)


if __name__ == "__main__":
    unittest.main()
