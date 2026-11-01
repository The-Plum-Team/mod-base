"""The per-tag release ledger: rows, transitions and the hidden block at the end of a release body."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from mod_base.release.ledger import (BLOCK_START, MAX_PLAN_ASSET_BYTES, MAX_RELEASE_BODY_BYTES, TRANSITIONS,
                                     TRUNCATION_LINE, advance, complete, new_ledger, parse_body, render_body,
                                     validate_ledger)
from mod_base.release.plan import MAX_RELEASE_PLAN_BYTES, plan_digest, validate_release_plan
from tests.test_release_plan import World


def derived(config_name: str = "config-quick-skin.json") -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        return World(Path(temporary), config_name).derive()


def large_plan(lanes: int, *, lane_chars: int = 80, changelog_bytes: int = 64 * 1024 - 16) -> dict:
    """One tag of ``lanes`` lanes with ids of ``lane_chars`` characters, a 128-character tag and a
    changelog of numbered lines close to the changelog bound: the largest ledger next to the
    largest changelog."""

    plan = derived("config-single-tag.json")
    tag = "v4.0.0" + "x" * 122
    template = plan["lanes"]["fabric-1.20.1"]
    jar = plan["tags"][0]["assets"][0]
    ids = [f"{index:03d}" + "a" * (lane_chars - 3) for index in range(lanes)]
    plan["tags"] = [{"tag": tag, "name": "Mod", "minecraft": None, "lanes": ids,
                     "assets": [{**jar, "lane": lane, "path": f"files/{index}.jar", "filename": f"{index}.jar"}
                                for index, lane in enumerate(ids)]}]
    plan["lanes"] = {}
    for index, lane in enumerate(ids):
        entry = copy.deepcopy(template)
        entry["tag"] = tag
        entry["modrinth"]["version_number"] = f"4.0.0+{index}"
        entry["curseforge"] = {"project_id": 1, "display_name": "Mod", "release_type": "release",
                               "game_versions": ["1.20.1"], "loaders": ["fabric"], "relations": []}
        plan["lanes"][lane] = entry
    lines, size = [], 0
    while size < changelog_bytes - 40:
        line = f"- line {len(lines):05d} " + "é" * 10
        lines.append(line)
        size += len(line.encode()) + 1
    text = "\n".join(lines)
    plan["changelog"] = {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return validate_release_plan(plan)


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = derived()
        cls.tag = "mc1.21.3-v4.0.0"

    def rejects(self, function, *args, message: str | None = None, **kwargs) -> None:
        with self.assertRaises(MbError) as caught:
            function(*args, **kwargs)
        if message is not None:
            self.assertIn(message, str(caught.exception))


class LedgerTests(Case):
    def test_a_new_ledger_has_one_unstarted_row_per_lane_and_marketplace(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        self.assertEqual(ledger, {"kind": "mod-base.release.ledger", "schema_version": 1, "tag": self.tag,
                                  "plan_sha256": plan_digest(self.plan), "rows": {
                                      key: {"state": "unstarted", "remote_id": None}
                                      for key in ("fabric-1.21.3--modrinth", "fabric-1.21.3--curseforge",
                                                  "neoforge-1.21.3--modrinth", "neoforge-1.21.3--curseforge")}})
        self.assertFalse(complete(ledger))
        self.rejects(new_ledger, self.plan, "mc9.9-v4.0.0", message="not a tag of the plan")

    def test_rows_follow_the_marketplaces_of_the_plan(self) -> None:
        plan = derived("config-single-tag.json")
        self.assertEqual(sorted(new_ledger(plan, "v4.0.0")["rows"]),
                         sorted(f"{lane}--modrinth" for lane in plan["lanes"]))

    def test_validation_is_strict_and_binds_the_plan_and_tag(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        validate_ledger(ledger, plan=self.plan, tag=self.tag)
        for mutate, message in (
                (lambda d: d.update(extra=1), "unknown keys"),
                (lambda d: d.update(schema_version=2), None),
                (lambda d: d.update(tag="v1..0"), "Git tag"),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(extra=1), "unknown keys"),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(state="done"), None),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(remote_id="AbC123"), "no remote id"),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(state="verified"), "names its remote id"),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(state="pending", remote_id="a-b"), None),
                (lambda d: d["rows"]["fabric-1.21.3--modrinth"].update(state="pending", remote_id="x" * 81), None),
                (lambda d: d["rows"].update({"x--github": {"state": "unstarted", "remote_id": None}}), "invalid key"),
                (lambda d: d["rows"].update({"a--b--modrinth": {"state": "unstarted", "remote_id": None}}),
                 "invalid lane"),
                (lambda d: d.update(rows={}), None),
        ):
            changed = copy.deepcopy(ledger)
            mutate(changed)
            with self.subTest(message=message, changed=changed):
                self.rejects(validate_ledger, changed, message=message)
        self.rejects(validate_ledger, ledger, tag="mc1.20.1-v4.0.0", message="not the ledger of this tag")
        other = copy.deepcopy(self.plan)
        other["version"] = "4.0.1"
        self.rejects(validate_ledger, ledger, plan=other, message="not the ledger of this plan")
        missing = copy.deepcopy(ledger)
        del missing["rows"]["neoforge-1.21.3--curseforge"]
        self.rejects(validate_ledger, missing, plan=self.plan, message="exactly one row")
        moved = copy.deepcopy(ledger)
        moved["tag"] = "mc1.20.1-v4.0.0"
        self.rejects(validate_ledger, moved, plan=self.plan, message="exactly one row")

    def test_the_plan_size_bound_is_exported(self) -> None:
        self.assertEqual(MAX_PLAN_ASSET_BYTES, MAX_RELEASE_PLAN_BYTES)
        self.assertEqual(BLOCK_START, "<!-- mod-base-release-ledger:v1")


class TransitionTests(Case):
    ROW = "fabric-1.21.3--modrinth"

    def ledger(self, state: str, remote_id: str | None = None) -> dict:
        ledger = new_ledger(self.plan, self.tag)
        ledger["rows"][self.ROW] = {"state": state, "remote_id": remote_id}
        return validate_ledger(ledger)

    def test_the_allowed_transitions(self) -> None:
        self.assertEqual(TRANSITIONS, {("unstarted", "uploading"), ("uploading", "pending"),
                                       ("uploading", "verified"), ("pending", "verified"), ("unstarted", "verified")})
        ledger = new_ledger(self.plan, self.tag)
        uploading = advance(ledger, self.ROW, "uploading")
        self.assertEqual(ledger["rows"][self.ROW]["state"], "unstarted")
        self.assertEqual(uploading["rows"][self.ROW], {"state": "uploading", "remote_id": None})
        pending = advance(uploading, self.ROW, "pending")
        self.assertEqual(advance(pending, self.ROW, "pending"), pending)
        verified = advance(pending, self.ROW, "verified", "AbC123")
        self.assertEqual(verified["rows"][self.ROW], {"state": "verified", "remote_id": "AbC123"})
        self.assertEqual(advance(verified, self.ROW, "verified", "AbC123"), verified)
        identified = advance(uploading, self.ROW, "pending", "AbC123")
        self.assertEqual(advance(identified, self.ROW, "pending", "AbC123"), identified)
        self.assertEqual(advance(identified, self.ROW, "verified", "AbC123")["rows"][self.ROW]["remote_id"], "AbC123")
        self.assertEqual(advance(uploading, self.ROW, "verified", "123")["rows"][self.ROW]["state"], "verified")
        self.assertEqual(advance(ledger, self.ROW, "verified", "4567")["rows"][self.ROW]["state"], "verified")

    def test_every_other_move_is_refused(self) -> None:
        for state, remote_id, target, target_id in (
                ("unstarted", None, "unstarted", None), ("unstarted", None, "pending", None),
                ("unstarted", None, "verified", None), ("unstarted", None, "uploading", "AbC"),
                ("uploading", None, "uploading", None), ("uploading", None, "unstarted", None),
                ("uploading", None, "verified", None),
                ("pending", None, "uploading", None), ("pending", None, "unstarted", None),
                ("pending", "AbC", "pending", "XyZ"), ("pending", "AbC", "pending", None),
                ("pending", "AbC", "verified", "XyZ"), ("pending", None, "pending", "AbC"),
                ("verified", "AbC", "pending", "AbC"), ("verified", "AbC", "uploading", None),
                ("verified", "AbC", "unstarted", None), ("verified", "AbC", "verified", "XyZ"),
                ("verified", "AbC", "verified", None),
        ):
            with self.subTest(state=state, remote_id=remote_id, target=target, target_id=target_id):
                self.rejects(advance, self.ledger(state, remote_id), self.ROW, target, target_id)

    def test_the_row_state_and_remote_id_are_checked(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        self.rejects(advance, ledger, "fabric-1.20.1--modrinth", "uploading", message="not a row")
        self.rejects(advance, ledger, self.ROW, "done", message="must be one of")
        for remote_id in ("", "a b", "x" * 81, 5, "id\n"):
            with self.subTest(remote_id=remote_id):
                self.rejects(advance, ledger, self.ROW, "verified", remote_id)

    def test_complete_means_every_row_verified(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        for index, row in enumerate(sorted(ledger["rows"])):
            self.assertFalse(complete(ledger))
            ledger = advance(ledger, row, "verified", f"id{index}")
        self.assertTrue(complete(ledger))


class BodyTests(Case):
    def test_the_body_is_the_changelog_a_blank_line_and_the_block(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        body = render_body(self.plan, self.tag, ledger)
        self.assertEqual(body, self.plan["changelog"]["text"] + "\n\n" + BLOCK_START + "\n"
                         + canonical_json(ledger).decode() + "-->")
        self.assertEqual(parse_body(body), ledger)
        self.assertEqual(parse_body(body, plan=self.plan, tag=self.tag), ledger)
        self.assertEqual(parse_body(body, plan=self.plan), ledger)

    def test_the_block_must_be_one_canonical_block_at_the_very_end(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        body = render_body(self.plan, self.tag, ledger)
        block = body[body.index(BLOCK_START):]
        json_text = canonical_json(ledger).decode()
        for name, changed in {
            "two blocks": body + "\n\n" + block,
            "a block in the notes": block + "\n\n" + body,
            "text after the block": body + "\n",
            "space after the block": body + " ",
            "no blank line": body.replace("\n\n" + BLOCK_START, "\n" + BLOCK_START),
            "block alone": block,
            "other version": body.replace(BLOCK_START, "<!-- mod-base-release-ledger:v2"),
            "pretty JSON": body.replace(json_text, json_text.replace(",", ", ")),
            "no final newline": body.replace(json_text + "-->", json_text.rstrip("\n") + "-->"),
            "CRLF": body.replace("\n", "\r\n"),
            "truncated": body[:-10],
            "not closed": body[:-3],
            "duplicate key": body.replace('"kind":"mod-base.release.ledger"',
                                          '"kind":"mod-base.release.ledger","kind":"mod-base.release.ledger"'),
            "oversized": "x" * MAX_RELEASE_BODY_BYTES + body,
            "lone surrogate": "\ud800" + body,
        }.items():
            with self.subTest(name=name):
                self.rejects(parse_body, changed)
        self.rejects(parse_body, None)
        self.rejects(parse_body, "")

    def test_with_the_plan_the_whole_body_is_bound(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        body = render_body(self.plan, self.tag, ledger)
        edited = body.replace("A new skin menu", "A changed skin menu")
        self.assertEqual(parse_body(edited), ledger)
        self.rejects(parse_body, edited, plan=self.plan, message="not the body of this plan")
        self.rejects(parse_body, body, plan=self.plan, tag="mc1.20.1-v4.0.0", message="not the ledger of this tag")
        other = copy.deepcopy(self.plan)
        other["version"] = "4.0.1"
        self.rejects(parse_body, body, plan=other, message="not the ledger of this plan")

    def test_render_refuses_a_ledger_of_another_tag_or_plan(self) -> None:
        ledger = new_ledger(self.plan, self.tag)
        self.rejects(render_body, self.plan, "mc1.20.1-v4.0.0", ledger)
        other = copy.deepcopy(self.plan)
        other["version"] = "4.0.1"
        self.rejects(render_body, other, self.tag, ledger)

    def test_a_changelog_too_long_for_the_largest_ledger_is_cut_at_a_line(self) -> None:
        plan = large_plan(256)
        tag = plan["tags"][0]["tag"]
        ledger = new_ledger(plan, tag)
        self.assertEqual(len(ledger["rows"]), 512)
        body = render_body(plan, tag, ledger)
        self.assertLessEqual(len(body.encode()), MAX_RELEASE_BODY_BYTES)
        notes = body[:body.index(BLOCK_START) - 2]
        self.assertTrue(notes.endswith("\n\n" + TRUNCATION_LINE))
        kept = notes[:-len("\n\n" + TRUNCATION_LINE)]
        self.assertTrue(plan["changelog"]["text"].startswith(kept + "\n"))
        self.assertGreater(len(kept), 1000)
        for index, row in enumerate(sorted(ledger["rows"])):
            ledger = advance(ledger, row, "verified", f"{index:03d}" + "Z" * 77)
        full = render_body(plan, tag, ledger)
        self.assertLessEqual(len(full.encode()), MAX_RELEASE_BODY_BYTES)
        self.assertEqual(full[:full.index(BLOCK_START)], body[:body.index(BLOCK_START)])
        self.assertEqual(parse_body(full, plan=plan), ledger)

    def test_a_changelog_that_fits_is_kept_whole(self) -> None:
        plan = large_plan(8)
        tag = plan["tags"][0]["tag"]
        body = render_body(plan, tag, new_ledger(plan, tag))
        self.assertTrue(body.startswith(plan["changelog"]["text"] + "\n\n" + BLOCK_START))


if __name__ == "__main__":
    unittest.main()
