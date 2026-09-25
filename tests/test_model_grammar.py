from __future__ import annotations

import re
import unittest

from mod_base.errors import MbError
from mod_base.model import grammar

SHA = "0123456789abcdef0123456789abcdef01234567"


class IdentifierGrammarTest(unittest.TestCase):
    def test_key(self) -> None:
        for good in ("mc1.20.1", "mc26.3", "0123456789abcdef01234567", "a1", "mc1_20", "a-b", "x" * 64):
            with self.subTest(good=good):
                self.assertTrue(grammar.is_key(good))
        for bad in ("mc--1", "-a", "a-", "a", "A1", "mc1.20.1/", "a b", "x" * 65, "", "a\n", 1, None):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_key(bad))

    def test_family(self) -> None:
        self.assertTrue(grammar.is_family("mod-compatibility"))
        self.assertTrue(grammar.is_family("a" * 32))
        for bad in ("a" * 33, "Mod", "1mod", "mod--compat", "mod-", "mod_compat", ""):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_family(bad))

    def test_lane_id_is_two_to_four_segments(self) -> None:
        self.assertTrue(grammar.is_lane_id("1.20.1-fabric/full"))
        self.assertTrue(grammar.is_lane_id("a/b/c/d"))
        for bad in ("fabric", "a/b/c/d/e", "/a/b", "a//b", "a/b/", "a b/c"):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_lane_id(bad))

    def test_hash_grammar(self) -> None:
        self.assertTrue(grammar.is_match(grammar.SHA1, SHA))
        self.assertFalse(grammar.is_match(grammar.SHA1, SHA.upper()))
        self.assertTrue(grammar.is_match(grammar.SHA256, "a" * 64))
        self.assertTrue(grammar.is_match(grammar.DIGEST, "sha256:" + "a" * 64))
        self.assertFalse(grammar.is_match(grammar.DIGEST, "a" * 64))

    def test_branch_is_bp_rule(self) -> None:
        for good in ("master", "1.21.1-neoforge-fabric", "release/1.20.1", "a.b_c-d"):
            self.assertTrue(grammar.is_match(grammar.BRANCH, good))
        for bad in ("/master", "a..b", "a//b", "a b", "", "x" * 201, "refs/heads/../x"):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_match(grammar.BRANCH, bad))

    def test_repository_never_traverses(self) -> None:
        for good in ("The-Plum-Team/Quick-Skin-Mod", "o/.github", "o/a.b_c-d", "o/...", "o_x/y", "a/b"):
            with self.subTest(good=good):
                self.assertTrue(grammar.is_match(grammar.REPOSITORY, good))
        for bad in ("../..", "./x", "../x", "o/.", "o/..", "-o/x", ".o/x", "o/x/y", "o", "/o/x", "o/", "o/x ",
                    "a" * 40 + "/x", "o/" + "x" * 101):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_match(grammar.REPOSITORY, bad))

    def test_repository_urls_and_workflow_refs_share_the_repository_grammar(self) -> None:
        self.assertEqual(grammar.run_url("o/.github", 5), "https://github.com/o/.github/actions/runs/5")
        for repository in ("../..", "o/..", "o/.", "./x"):
            with self.subTest(repository=repository):
                with self.assertRaises(MbError):
                    grammar.run_url(repository, 1)
                with self.assertRaises(MbError):
                    grammar.workflow_ref(repository, ".github/workflows/pages.yml", "master")
                with self.assertRaises(MbError):
                    grammar.parse_workflow_ref(f"{repository}/.github/workflows/pages.yml@refs/heads/master")
                self.assertFalse(grammar.is_match(grammar.RUN_URL, f"https://github.com/{repository}/actions/runs/1"))
        self.assertTrue(grammar.is_match(grammar.RUN_URL, "https://github.com/o/r/actions/runs/1"))

    def test_extension_names(self) -> None:
        for good in ("quick-skin.runtime_source", "block-pops.aggregate_scope"):
            self.assertTrue(grammar.is_extension_name(good))
        for bad in ("QuickSkin.runtime", "quick-skin", "quick.skin.x", "quick-skin.Runtime"):
            self.assertFalse(grammar.is_extension_name(bad))

    def test_require_positive_int_rejects_bool(self) -> None:
        self.assertEqual(grammar.require_positive_int(5, "n"), 5)
        for bad in (True, 0, -1, 1.0, "1"):
            with self.subTest(bad=bad), self.assertRaises(MbError):
                grammar.require_positive_int(bad, "n")


class PathGrammarTest(unittest.TestCase):
    def test_bundle_paths(self) -> None:
        for good in ("manifest.json", "runtime/profiles/a/result.json", "images/abc.webp", ".nojekyll"):
            self.assertTrue(grammar.is_bundle_path(good), good)
        for bad in ("", "/abs", "a/../b", "./a", "a/./b", "a//b", "a\\b", "a/b/", "..", "a\x00b", "a b", "é",
                    "/".join(["a"] * 17), "a" * 301):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_bundle_path(bad))

    def test_repo_paths_refuse_git_directories(self) -> None:
        self.assertTrue(grammar.is_repo_path(".github/workflows/pages.yml"))
        self.assertTrue(grammar.is_repo_path(".gitignore"))
        for bad in (".git/config", "a/.git/hooks/x", ".GIT/config", "../x"):
            with self.subTest(bad=bad):
                self.assertFalse(grammar.is_repo_path(bad))


class RunIdentityTest(unittest.TestCase):
    def test_run_url(self) -> None:
        self.assertEqual(grammar.run_url("The-Plum-Team/Quick-Skin-Mod", 12),
                         "https://github.com/The-Plum-Team/Quick-Skin-Mod/actions/runs/12")
        with self.assertRaises(MbError):
            grammar.run_url("not a repo", 12)
        with self.assertRaises(MbError):
            grammar.run_url("o/r", 0)

    def test_workflow_ref_round_trip(self) -> None:
        value = grammar.workflow_ref("o/r", ".github/workflows/pages.yml", "master")
        self.assertEqual(value, "o/r/.github/workflows/pages.yml@refs/heads/master")
        parsed = grammar.parse_workflow_ref(value)
        self.assertEqual((parsed.repository, parsed.path, parsed.branch), ("o/r", ".github/workflows/pages.yml", "master"))

    def test_workflow_ref_refuses_tags_pulls_and_traversal(self) -> None:
        for bad in ("o/r/.github/workflows/pages.yml@refs/tags/v1", "o/r/.github/workflows/pages.yml@refs/pull/1/merge",
                    "o/r/.github/workflows/pages.yml@refs/heads/a..b", "o/r/.github/actions/x.yml@refs/heads/m",
                    "o/r/.github/workflows/pages.yml"):
            with self.subTest(bad=bad), self.assertRaises(MbError):
                grammar.parse_workflow_ref(bad)

    def test_timestamps(self) -> None:
        self.assertEqual(grammar.parse_timestamp("2026-09-25T10:00:00Z").isoformat(), "2026-09-25T10:00:00+00:00")
        for bad in ("2026-09-25T10:00:00+00:00", "2026-02-30T10:00:00Z", "2026-09-25 10:00:00Z", "", None):
            with self.subTest(bad=bad), self.assertRaises(MbError):
                grammar.parse_timestamp(bad)

    def test_short_sha(self) -> None:
        self.assertEqual(grammar.short_sha(SHA), SHA[:12])
        with self.assertRaises(MbError):
            grammar.short_sha(SHA, 6)


class ArtifactNameTest(unittest.TestCase):
    def test_builders_and_parser_round_trip(self) -> None:
        cases = [
            (grammar.handoff_name("mc1.20.1", 2), dict(kind="handoff", key="mc1.20.1", attempt=2)),
            (grammar.anchor_name("mc1.20.1", SHA, 123, 1),
             dict(kind="anchor", key="mc1.20.1", commit=SHA, run_id=123, attempt=1)),
            (grammar.family_handoff_name("mod-compatibility", "mc26.3", 3),
             dict(kind="family-handoff", family="mod-compatibility", key="mc26.3", attempt=3)),
            (grammar.collected_name("mc1.20.1"), dict(kind="collected", key="mc1.20.1")),
            (grammar.collected_family_name("mod-compatibility", "mc1.20.1"),
             dict(kind="collected-family", family="mod-compatibility", key="mc1.20.1")),
            (grammar.cache_name("0123456789abcdef01234567", SHA),
             dict(kind="cache", key="0123456789abcdef01234567", coverage_sha=SHA)),
            (grammar.family_cache_name("mod-compatibility", "mc1.20.1", SHA),
             dict(kind="family-cache", family="mod-compatibility", key="mc1.20.1", coverage_sha=SHA)),
            (grammar.baseline_name("mc1.20.1", SHA, 77), dict(kind="baseline", key="mc1.20.1", commit=SHA, run_id=77)),
            ("mb-promotion", dict(kind="promotion")),
            ("github-pages", dict(kind="pages")),
        ]
        for name, fields in cases:
            with self.subTest(name=name):
                parsed = grammar.parse_artifact_name(name)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.name, name)
                for field, value in fields.items():
                    self.assertEqual(getattr(parsed, field), value)
                self.assertTrue(grammar.is_kit_artifact_name(name))

    def test_exact_names(self) -> None:
        self.assertEqual(grammar.handoff_name("mc1.20.1", 1), "mb-handoff--mc1.20.1--a1")
        self.assertEqual(grammar.anchor_name("k1", SHA, 9, 2), f"mb-anchor--k1--{SHA}--9--a2")
        self.assertEqual(grammar.family_handoff_name("f", "k1", 1), "mb-family-handoff--f--k1--a1")
        self.assertEqual(grammar.collected_name("k1"), "mb-collected--k1")
        self.assertEqual(grammar.collected_family_name("f", "k1"), "mb-collected-family--f--k1")
        self.assertEqual(grammar.cache_name("k1", SHA), f"mb-cache--k1--{SHA}")
        self.assertEqual(grammar.family_cache_name("f", "k1", SHA), f"mb-family-cache--f--k1--{SHA}")
        self.assertEqual(grammar.baseline_name("k1", SHA, 5), f"mb-baseline--k1--{SHA}--5")

    def test_keys_that_look_like_attempts_stay_unambiguous(self) -> None:
        parsed = grammar.parse_artifact_name("mb-handoff--a1--a2")
        assert parsed is not None
        self.assertEqual((parsed.key, parsed.attempt), ("a1", 2))

    def test_foreign_and_malformed_names_are_not_kit_names(self) -> None:
        for name in (
            "pages-cache-master", "pages-e2e-mc1.20.1", "visual-anchor-v1-x", "collected-mc1.20.1",
            "mb-handoff--mc1.20.1", "mb-handoff--mc1.20.1--a0", "mb-handoff--mc1.20.1--1", "mb-handoff--MC--a1",
            "mb-handoff--mc1.20.1--a01", "mb-handoff--mc1.20.1--a1--extra", "mb-cache--k1--short",
            f"mb-cache--k1--{SHA.upper()}", "mb-promotion--x", "xmb-promotion", "mb-anchor--k1--x--1--a1",
            f"mb-anchor--k1--{SHA}--0--a1", f"mb-baseline--k1--{SHA}--01", "mb-family-cache--F--k1--" + SHA,
            "mb-collected--", "mb-unknown--k1", "", None, 5,
        ):
            with self.subTest(name=name):
                self.assertIsNone(grammar.parse_artifact_name(name))
                self.assertFalse(grammar.is_kit_artifact_name(name))

    def test_require_artifact_name_checks_kind(self) -> None:
        self.assertEqual(grammar.require_artifact_name("mb-handoff--k1--a1", "handoff").key, "k1")
        with self.assertRaises(MbError):
            grammar.require_artifact_name("mb-handoff--k1--a1", "cache")
        with self.assertRaises(MbError):
            grammar.require_artifact_name("pages-cache-master", "cache")

    def test_builders_validate_every_part(self) -> None:
        for call in (
            lambda: grammar.handoff_name("MC", 1),
            lambda: grammar.handoff_name("k1", 0),
            lambda: grammar.handoff_name("k1", True),
            lambda: grammar.anchor_name("k1", "short", 1, 1),
            lambda: grammar.family_handoff_name("Bad", "k1", 1),
            lambda: grammar.cache_name("k1", "x" * 40),
            lambda: grammar.baseline_name("k1", SHA, -1),
            lambda: grammar.handoff_name("k1", 1001),
        ):
            with self.assertRaises(MbError):
                call()

    def test_names_stay_within_the_github_bound(self) -> None:
        longest = grammar.anchor_name("k" * 64, SHA, 2**63 - 1, 1000)
        self.assertLessEqual(len(longest.encode()), 240)

    def test_baseline_regex_equals_the_quick_skin_literal(self) -> None:
        literal = r"^mb-baseline--mc[0-9]+(?:\.[0-9]+){1,2}--[0-9a-f]{40}--[1-9][0-9]*$"
        self.assertEqual(grammar.baseline_name_regex(r"mc[0-9]+(?:\.[0-9]+){1,2}"), literal)
        pattern = re.compile(literal)
        self.assertIsNotNone(pattern.fullmatch(grammar.baseline_name("mc1.20.1", SHA, 12)))
        self.assertIsNone(pattern.fullmatch(grammar.cache_name("mc1.20.1", SHA)))
        with self.assertRaises(MbError):
            grammar.baseline_name_regex("")


if __name__ == "__main__":
    unittest.main()
