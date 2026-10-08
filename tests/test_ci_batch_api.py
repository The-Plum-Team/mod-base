"""The GitHub writes batches need: ``patch_json`` of the client, and the fake's routes for pull
requests, issue comments and ref deletion. Each is refused unless the client was built writable."""

from __future__ import annotations

import unittest

from mod_base.errors import MbError
from mod_base.github.api import ApiError, ApiNotFound, ReadOnlyViolation
from mod_base.github.fake import FakeGitHub
from tests.test_github_api import REPOSITORY, ClientTestCase, Response, http_error

PULL = f"/repos/{REPOSITORY}/pulls/7"


class PatchJsonTests(ClientTestCase):
    def test_a_read_only_client_refuses_before_the_network(self):
        client, opener, _ = self.client([])
        with self.assertRaises(ReadOnlyViolation):
            client.patch_json(PULL, {"state": "closed"})
        self.assertEqual((opener.requests, client.request_count), ([], 0))

    def test_a_writable_client_patches_canonical_json_and_returns_the_answer(self):
        client, opener, _ = self.client([Response(body=b'{"number":7,"state":"closed"}')], writable=True)
        self.assertEqual(client.patch_json(PULL, {"state": "closed", "body": "b"}), {"number": 7, "state": "closed"})
        request = opener.requests[0]
        self.assertEqual((request.get_method(), request.data), ("PATCH", b'{"body":"b","state":"closed"}\n'))
        self.assertEqual(request.get_header("Content-type"), "application/json")
        for outcome, error in ((Response(status=201, body=b"{}"), ApiError), (Response(body=b"not json"), ApiError),
                               (http_error(404), ApiNotFound), (http_error(422), ApiError)):
            client, _, sleeps = self.client([outcome], writable=True)
            with self.subTest(outcome=type(outcome).__name__), self.assertRaises(error):
                client.patch_json(PULL, {"state": "closed"})
            self.assertEqual(sleeps, [])
        client, opener, _ = self.client([], writable=True)
        with self.assertRaises(MbError):
            client.patch_json(PULL, ["state"])  # type: ignore[arg-type]
        self.assertEqual(opener.requests, [])


class FakeWriteTests(unittest.TestCase):
    def fake(self, **options) -> FakeGitHub:
        api = FakeGitHub(repository="example/mod", **options)
        api.set_branch("master", "1" * 40, "2" * 40)
        api.add_ref("heads/fix/a", "3" * 40)
        api.add_pull({"number": 5, "state": "open", "title": "t", "body": "", "merged": False,
                      "head": {"ref": "fix/a", "sha": "3" * 40}, "base": {"ref": "master"}})
        return api

    def test_every_write_is_refused_on_a_read_only_fake(self):
        api = self.fake()
        for call in (lambda: api.post_json("/repos/example/mod/pulls", {}),
                     lambda: api.post_json("/repos/example/mod/issues/5/comments", {"body": "x"}),
                     lambda: api.patch_json("/repos/example/mod/pulls/5", {"state": "closed"}),
                     lambda: api.delete("/repos/example/mod/git/refs/heads/fix/a")):
            with self.assertRaises(ReadOnlyViolation):
                call()
        self.assertEqual((api.request_count, api.mutations), (0, []))
        self.assertEqual(api.get_json("/repos/example/mod/pulls/5")["state"], "open")

    def test_pull_requests_are_created_changed_and_served(self):
        api = self.fake(writable=True)
        api.add_ref("heads/batch/x", "4" * 40)
        payload = {"title": "chore: batch", "head": "batch/x", "base": "master", "body": "text", "draft": False}
        created = api.post_json("/repos/example/mod/pulls", payload)
        self.assertEqual((created["number"], created["state"], created["draft"], created["head"], created["base"]), (
            6, "open", False, {"ref": "batch/x", "sha": "4" * 40, "repo": {"full_name": "example/mod"}},
            {"ref": "master", "sha": "1" * 40, "repo": {"full_name": "example/mod"}}))
        self.assertEqual(api.get_json("/repos/example/mod/pulls/6"), created)
        created["state"] = "mutated"
        self.assertEqual(api.get_json("/repos/example/mod/pulls/6")["state"], "open")
        for bad in (payload, {**payload, "head": "missing"}, {**payload, "base": "missing"},
                    {**payload, "head": ["batch/x"]}, {key: payload[key] for key in payload if key != "draft"},
                    {**payload, "draft": "no"}, {**payload, "title": ""}, {**payload, "extra": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ApiError, "HTTP 422"):
                api.post_json("/repos/example/mod/pulls", bad)
        closed = api.patch_json("/repos/example/mod/pulls/6", {"state": "closed"})
        self.assertEqual((closed["state"], api.get_json("/repos/example/mod/pulls/6")["state"]), ("closed", "closed"))
        self.assertEqual(api.post_json("/repos/example/mod/pulls", payload)["number"], 7)
        api.add_pull({**api.get_json("/repos/example/mod/pulls/6"), "merged": True})
        for path, bad in (("/repos/example/mod/pulls/6", {"state": "open"}), ("/repos/example/mod/pulls/5", {}),
                          ("/repos/example/mod/pulls/5", {"state": "merged"}),
                          ("/repos/example/mod/pulls/5", {"draft": True}), ("/repos/example/mod/pulls/5", {"title": 1})):
            with self.subTest(bad=bad), self.assertRaisesRegex(ApiError, "HTTP 422"):
                api.patch_json(path, bad)
        for path in ("/repos/example/mod/pulls/99", "/repos/other/mod/pulls/5", "/repos/example/mod/issues/5"):
            with self.subTest(path=path), self.assertRaises(ApiNotFound):
                api.patch_json(path, {"state": "closed"})
        with self.assertRaises(ApiNotFound):
            api.get_json("/repos/example/mod/pulls/99")
        self.assertEqual([mutation[:2] for mutation in api.mutations],
                         [("POST", "/repos/example/mod/pulls"), ("PATCH", "/repos/example/mod/pulls/6"),
                          ("POST", "/repos/example/mod/pulls")])

    def test_comments_and_ref_deletion(self):
        api = self.fake(writable=True)
        comment = api.post_json("/repos/example/mod/issues/5/comments", {"body": "Merged through batch #9."})
        self.assertEqual(comment["body"], "Merged through batch #9.")
        for path, payload, error in (("/repos/example/mod/issues/5/comments", {"body": ""}, ApiError),
                                     ("/repos/example/mod/issues/5/comments", {"text": "x"}, ApiError),
                                     ("/repos/example/mod/issues/99/comments", {"body": "x"}, ApiNotFound),
                                     ("/repos/other/mod/issues/5/comments", {"body": "x"}, ApiNotFound)):
            with self.subTest(path=path, payload=payload), self.assertRaises(error):
                api.post_json(path, payload)
        api.add_ref("heads/topic", "5" * 40)
        api.set_branch("topic", "5" * 40, "6" * 40)
        api.delete("/repos/example/mod/git/refs/heads/fix/a")
        api.delete("/repos/example/mod/git/refs/heads/topic")
        for path in ("/repos/example/mod/git/ref/heads/fix/a", "/repos/example/mod/branches/topic"):
            with self.assertRaises(ApiNotFound):
                api.get_json(path)
        with self.assertRaisesRegex(ApiError, "HTTP 422"):
            api.delete("/repos/example/mod/git/refs/heads/fix/a")
        with self.assertRaises(ApiNotFound):
            api.delete("/repos/example/mod/git/refs")
        self.assertEqual(api.get_json("/repos/example/mod/branches/master")["commit"]["sha"], "1" * 40)
        self.assertEqual([mutation[:2] for mutation in api.mutations],
                         [("POST", "/repos/example/mod/issues/5/comments"),
                          ("DELETE", "/repos/example/mod/git/refs/heads/fix/a"),
                          ("DELETE", "/repos/example/mod/git/refs/heads/topic")])


if __name__ == "__main__":
    unittest.main()
